# Copyright (c) 2018, 2019, 2020 President and Fellows of Harvard College.
# This file is part of ProvBuild.

"""'update' command"""
from __future__ import (absolute_import, print_function,
                        division, unicode_literals)

import os
import sys
import argparse
from future.utils import viewitems

from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy import ForeignKeyConstraint, select, func, distinct
from ..persistence import relational, content, persistence_config

from ..persistence.models.base import AlchemyProxy, proxy_class, query_many_property, proxy_gen
from ..persistence.models.base import one, many_ref, many_viewonly_ref, backref_many, is_none
from ..persistence.models.base import proxy


from ..utils import io, metaprofiler
from ..collection.metadata import Metascript
from ..persistence.models import Tag, Trial, FunctionDef, Module, Dependency, FileAccess, EnvironmentAttr, Object, Activation, ObjectValue, Variable, VariableDependency, VariableUsage
from ..persistence import persistence_config, content
from ..utils.io import print_msg
from .command import Command

import linecache

def debug_print(string, content, arg=False):
    if arg == True:
        print('{} is {}'.format(string, content))
    return

def debug_detail_print(string, arg=False):
    if arg == True:
        print(string)
    return

def non_negative(string):
    """Check if argument is >= 0"""
    value = int(string)
    if value < 0:
        raise argparse.ArgumentTypeError(
            "{} is not a non-negative integer value".format(string))
    return value

def get_closest_graybox(fileid, result_variable):
    res = -1
    for i in range(fileid-1, 0, -1):
        if result_variable[i].name == '--graybox--':
            res = i;
            break;
    return (res+1) ### actual variable id needs +1

# check if content contains if(return 1), elif(return 2), else(return 3)
def check_cond(line, result_functiondef):
    res = []
    for i in result_functiondef:
        if line not in res and line >= i.first_line and line <= i.last_line:
            for j in range(i.first_line, i.last_line+1):
                res.append(j)
    return res
    
# given current line, check its function definition ID (or cond ID or loop ID)
def check_def_id(line, result_functiondef):
    ret_id = []
    for i in result_functiondef:
        if line >= i.first_line and line <= i.last_line:
            ret_id.append(i.id)
    if len(ret_id) > 1:
        tmp = ret_id[0]
        for i in range(1, len(ret_id)):
            cur = ret_id[i]
            if result_functiondef[cur-1].first_line < result_functiondef[tmp-1].first_line:
                tmp = cur
            else:
                continue
        return tmp
    elif len(ret_id) == 1:
        return ret_id[0]
    else:
        return 0

def check_related_call(name, result_variable):
    ret = []
    name_length = len(name)
    for r in result_variable:
        if r.name[0:name_length] == name[0:name_length] and r.type == 'call':
            ret.append(r.id)
    return ret

def check_related_arg(line, result_variable):
    ret = []
    for r in result_variable:
        if r.type == 'arg' and r.line == line:
            ret.append(r.id)
    return ret

def check_related_call_general(name, line, result_variable):
    ret = []
    for r in result_variable:
        if r.type == 'call' and r.line == line:
            ret.append(r.id)
    return ret

def remove_loop_cond_funcdef(funclist):
    func_list_remove = []
    for i in funclist:
        if 'LOOP_STMT' in i:
            func_list_remove.append(i)
        elif 'CONDITIONAL_STMT' in i:
            func_list_remove.append(i)
    for i in func_list_remove:
        funclist.remove(i)
    return funclist

class Update(Command):
    """ Create ProvScript based on the user input """

    def __init__(self, *args, **kwargs):
        super(Update, self).__init__(*args, **kwargs)

    def add_arguments(self):
        add_arg = self.add_argument

        add_arg("--dir", type=str,
                help="set project path where is the database. Default to "
                     "current directory")
        add_arg("-t", "--trial", type=non_negative,
                help="get the previous trial id")
        add_arg("-fn", "--funcname", type=str,
                help="function name input")
        add_arg("-vn", "--varname", type=str,
                help="variable name input")
        add_arg("--debug", type = int, default=0, help="enable debug")
        add_arg("--morefunc", type=str, default='',
                help="undefined function")

    def execute(self, args):
        # first, we need to restore the metascript based on trial id
        persistence_config.connect_existing(args.dir or os.getcwd())
        metascript = Metascript().read_restore_args(args)
        self.trial = trial = metascript.trial = Trial(trial_ref=args.trial)
        metascript.trial_id = trial.id
        metascript.name = trial.script
        metascript.fake_path(trial.script, "")
        metascript.paths[trial.script].code_hash = None

        metascript.trial_id = Trial.store(
            *metascript.create_trial_args(
                args="<update {}>".format(metascript.trial_id), run=False
            )
        )
        debug_mode = args.debug
        more_func_name = args.morefunc
        debug_print("undefined function name", more_func_name, debug_mode)

        # trial table
        result_trial = trial.pull_content(trial.id)
        trial_table = trial.__table__

        # definition provenance
        # function_def table
        function_def = FunctionDef(trial_ref=args.trial)
        if function_def is not None:
            result_functiondef = function_def.pull_content(trial.id)
            function_def_table = function_def.__table__

            # collect all the function definitions
            func_defs = []
            for r in result_functiondef:
                func_defs.append(r.name)
        else:
            result_functiondef = []
            func_defs = []

        if args.funcname is not None:
            if args.funcname not in func_defs:
                print ("given function doesn't exist!")
                return

        # variable table
        variable = Variable(trial_ref=args.trial)
        result_variable = variable.pull_content(trial.id)
        variable_table = variable.__table__

        # get the global variable declarations here
        var_defs = []
        # get func graybox list here
        func_graybox = []
        for r in result_variable:
            if r.activation_id == 1 and r.type == 'normal':
                var_defs.append(r.name)
            if r.type == '--funcgraybox--':
                func_graybox.append(r.id)
        var_defs = list(set(var_defs))
        if args.varname is not None:
            if args.varname not in var_defs:
                print ("given variable doesn't exist!")
                return

        # variable_dependency table
        variable_dependency = VariableDependency(trial_ref=args.trial)
        result_variabledependency = variable_dependency.pull_content(trial.id)
        variable_dependency_table = variable_dependency.__table__

        # variable_usage table
        variable_usage = VariableUsage(trial_ref=args.trial)
        result_variableusage = variable_usage.pull_content(trial.id)
        variable_usage_table = variable_usage.__table__

        # function_activation table
        function_activation = Activation(trial_ref=args.trial)
        if function_activation is not None:
            result_functionactivation = function_activation.pull_content(trial.id)
            function_activation_table = function_activation.__table__
        else:
            result_functionactivation = []
        # if the input is function name, we are dealing with function
        if args.funcname is not None:
            given_funcname = args.funcname
            debug_print("given function name", given_funcname)
        # this time, we are dealing with variable instead of function
        else:
            given_varname = args.varname
            debug_print("given variable name", given_varname)

        #########################################
        # print("I think we are done with the provenance part")
        # print("So, now we will start to create a ProvScript for you")
        #########################################

        origin_file = open(metascript.name, "r")
        ### open a new file to store sub script
        update_file = open("ProvScript.py", "w")

        ### function definition bound
        update_file.write("# This is the function declaration part\n")
        update_file.write("# - Your previous script contains the following function definitions:\n")
        func_defs_update = remove_loop_cond_funcdef(func_defs)
        func_defs_str = ""
        for f in func_defs_update:
            func_defs_str = func_defs_str + '###' + f + '\n'
        update_file.write(func_defs_str)

        update_file.write("# This is the global variable declaration part\n")
        update_file.write("# - Your previous script contains the following global variable:\n")
        var_defs_str = ""
        for f in var_defs:
            var_defs_str = var_defs_str + '###' + f + '\n'
        update_file.write(var_defs_str)

        ### first, we will deal with the function name input
        if args.funcname is not None:
            # try to find the function name when it appears
            funcid = []
            funcid_copy = []
            given_funcname_list = []
            given_funcname_list.append(given_funcname)
            for i in result_functionactivation:
                if i.name in given_funcname_list and i.caller_id != 1:
                    caller_name = result_functionactivation[i.caller_id-1].name
                    if caller_name not in given_funcname_list:
                        given_funcname_list.append(caller_name)
            debug_print("Given function name list (including global functions)", given_funcname_list, debug_mode)
            for i in given_funcname_list:
                funcid += check_related_call(i, result_variable)

            funcid_calls = []
            # make a copy
            for i in funcid:
                funcid_copy.append(i)
                funcid_calls.append(i)

            debug_print("function ID", funcid, debug_mode)

            funcid_end = []
            for f in funcid_copy:
                for r in result_variabledependency:
                    if r.source_id == f and r.target_id not in funcid_copy:
                        debug_detail_print(">>> target: {} <- {}, type = {}, target type = {}".format(r.source_id, r.target_id, r.type, result_variable[r.target_id-1].type), debug_mode)
                        if result_variable[r.target_id-1].type == 'function definition':
                            pass
                        elif result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                            if 'function' in result_variable[r.target_id-1].value:
                                funcid_copy.append(r.target_id)
                            elif r.target_id not in funcid_end:
                                funcid_end.append(r.target_id)
                        elif result_variable[r.target_id-1].type == 'builtin':
                            if r.target_id not in funcid_end:
                                funcid_end.append(r.target_id)
                        else:
                            funcid_copy.append(r.target_id)
                            if result_variable[r.target_id-1].type == 'call' and result_variable[r.target_id-1].activation_id == 1:
                                same_call = check_related_call_general(result_variable[r.target_id-1].name, result_variable[r.target_id-1].line, result_variable)
                                debug_print("call in the same line", same_call, debug_mode)
                                for i in same_call:
                                    if i not in funcid_copy:
                                        debug_detail_print ("we add more calls here: {}".format(i), debug_mode)
                                        funcid_copy.append(i)
                                    if i not in funcid_calls:
                                        funcid_calls.append(i)

            debug_print("function ID sub list(updated target_id)", funcid_copy, debug_mode)
            debug_print("function ID end list (updated target_id)", funcid_end, debug_mode)
            debug_print("function ID related call list (updated target_id)", funcid_calls, debug_mode)
            debug_print("function ID list (updated target_id)", funcid_copy, debug_mode)

            for v in funcid_copy:
                for r in result_variabledependency:
                    if r.target_id == v and r.source_id not in funcid_copy:
                        debug_detail_print(">>> source: {} -> {}, type = {}, source type = {}".format(r.target_id, r.source_id, r.type, result_variable[r.source_id-1].type), debug_mode)
                        if result_variable[r.source_id-1].type == '--blackbox--':
                            debug_detail_print(">>> PASS: source: {} -> {}, type = {},  source type = {}".format(r.target_id, r.source_id, r.type, result_variable[r.source_id-1].type), debug_mode)
                            pass
                        else:
                            funcid_copy.append(r.source_id)
                            if result_variable[r.source_id-1].type == 'call' and result_variable[r.source_id-1].activation_id == 1:
                                same_call = check_related_call_general(result_variable[r.source_id-1].name, result_variable[r.source_id-1].line, result_variable)
                                debug_print("call in the same line", same_call)
                                for i in same_call:
                                    if i not in funcid_copy:
                                        debug_detail_print ("we add more calls here: {}".format(i), debug_mode)
                                        funcid_copy.append(i)
                                    if i not in funcid_calls:
                                        funcid_calls.append(i)
            debug_print("function ID list (updated source_id)", funcid_copy, debug_mode)
            debug_print("function ID related call list (updated source_id)", funcid_calls, debug_mode)

            for i in funcid_calls:
                for r in result_variabledependency:
                    if r.source_id == i:
                        debug_detail_print(">>> call: {} <- {}, type = {}, target type = {}".format(r.source_id, r.target_id, r.type, result_variable[r.target_id-1].type), debug_mode)
                        if r.type == 'return' and r.target_id not in funcid_calls:
                            funcid_calls.append(r.target_id)
                        elif r.type == 'parameter':
                            if result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in funcid_end and r.target_id not in funcid_copy:
                                    funcid_end.append(r.target_id)
                            elif r.target_id not in funcid_calls:
                                funcid_calls.append(r.target_id)
                        elif r.type == 'direct':
                            if result_variable[r.target_id-1].type == 'function definition':
                                pass
                            elif 'function' in result_variable[r.target_id-1].value and r.target_id not in funcid_copy:
                                funcid_copy.append(r.target_id)
                            elif result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in funcid_end and r.target_id not in funcid_copy:
                                    funcid_end.append(r.target_id)
                            elif r.target_id not in funcid_calls:
                                funcid_calls.append(r.target_id)
                        elif r.target_id not in funcid_calls:
                            funcid_calls.append(r.target_id)
            debug_print("function ID related call list (updated source_id)", funcid_calls, debug_mode)
            debug_print("function ID end list (updated source_id)", funcid_end, debug_mode)
            debug_print("function ID list (updated source_id)", funcid_copy, debug_mode)
            funcids = funcid_copy

            loop_list = []
            cond_list = []
            for v in funcids:
                for r in result_variabledependency:
                    if r.source_id == v:
                        if r.type == "loop":
                            debug_detail_print(">>> loop: {} <- {}, type = {}".format(r.source_id, r.target_id, r.type), debug_mode)
                            loop_list.append(r.target_id)
                            if r.target_id not in funcids:
                                funcids.append(r.target_id)
                        if r.type == "conditional":
                            debug_detail_print(">>> cond: {} <- {}, type = {}".format(r.source_id, r.target_id, r.type), debug_mode)
                            cond_list.append(r.target_id)
                            if result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in funcid_end and r.target_id not in funcids:
                                    funcid_end.append(r.target_id)
                            elif r.target_id not in funcids:
                                funcids.append(r.target_id)
            debug_print("function ID list (with loop and cond)", funcids, debug_mode)
            debug_print("loop list", loop_list, debug_mode)
            debug_print("cond list", cond_list, debug_mode)
            debug_print("variable end ID list", funcid_end, debug_mode)

            activations = []
            for i in funcids:
                actid = result_variable[i-1].activation_id
                if actid > 1:
                    if actid not in activations:
                        activations.append(actid)
            debug_print("activation list", activations, debug_mode)

            graybox_funcid = []
            for i in func_graybox:
                actid = result_variable[i-1].activation_id
                if actid in activations:
                    if i not in graybox_funcid:
                        graybox_funcid.append(i)
                    if i not in funcids:
                        funcids.append(i)
            debug_print("graybox variable list", graybox_funcid, debug_mode)

            for v in funcids:
                if result_variable[v-1].type == '--funcgraybox--':
                    graybox_funcid.append(v)
            debug_print("graybox variable list (updated)", graybox_funcid, debug_mode)

            # related functions' parameters
            func_params = []
            for v in graybox_funcid:
                for r in result_variabledependency:
                    if r.source_id == v:
                        if r.type == "parameter":
                            if result_variable[r.target_id-1].activation_id == 1 and r.target_id not in func_params:
                                func_params.append(r.target_id)
            debug_print("function ID list (graybox updated)", funcids, debug_mode)
            debug_print("function param list", func_params, debug_mode)
            func_params_remove = []
            func_params_add = []
            func_params_name = []
            for i in func_params:
                func_params_name.append(result_variable[i-1].name)

            funcid_end.sort()
            for i in funcid_end:
                if result_variable[i-1].name not in func_params_name and i not in func_params_add:
                    func_params_add.append(i)
            debug_print("function param add list", func_params_add, debug_mode)

            for i in funcid_end:
                for j in func_params:
                    if result_variable[i-1].name == result_variable[j-1].name and i < j:
                        if j not in func_params_remove:
                            func_params_remove.append(j)
                        if i not in func_params and i not in func_params_add:
                            func_params_add.append(i)
            debug_print("function param add list (updated)", func_params_add, debug_mode)

            func_params += func_params_add

            func_params.sort()
            debug_print("function param list (updated)", func_params, debug_mode)
            for i in func_params:
                for j in func_params:
                    # variable replication
                    if i != j and result_variable[i-1].name == result_variable[j-1].name:
                        if i < j and j not in func_params_remove:
                            func_params_remove.append(j)
            debug_print("function param remove list", func_params_remove, debug_mode)

            # remove those 'buildin' variables
            for i in func_params:
                if result_variable[i-1].type != 'normal' and i not in func_params_remove:
                    func_params_remove.append(i)

            normal_should_be_added = []
            for i in func_params_remove:
                func_params.remove(i) 
                if i not in funcids and result_variable[i-1].type == 'normal':
                    normal_should_be_added.append(i)
            debug_print("function param list (updated)", func_params, debug_mode)
            debug_print("normal variable (should be added later)", normal_should_be_added, debug_mode)

            related_funcdef_list = []
            funcids_remove = []
            for v in funcids:
                if result_variable[v-1].activation_id != 0: 
                    ### this means it belongs to some functions (or loop or cond or even global functions), we will include their definitions
                    current_line = result_variable[v-1].line
                    belong_funcdef = check_def_id(current_line, result_functiondef)
                    if belong_funcdef != 0:
                        # include all the related function definition list.
                        if belong_funcdef not in related_funcdef_list:
                            related_funcdef_list.append(belong_funcdef)
                        # remove those called by some function, we will include all the function definition
                        if result_variable[v-1].activation_id != 1:
                            funcids_remove.append(v)
                        # remove those function definition, we will include them 
                        if result_variable[v-1].type == 'function definition':
                            funcids_remove.append(v)

            debug_print("funcid remove list", funcids_remove, debug_mode)
            debug_print("related function definition list", related_funcdef_list, debug_mode)

            for i in funcids_remove:
                funcids.remove(i)
            for i in graybox_funcid:
                if i in funcids:
                    funcids.remove(i)
            debug_print("function ID list (updated)", funcids, debug_mode)

            funcids_remove = []
            related_func_calls = []
            normal_funcid = []
            arg_funcid = []
            for v in funcids:
                if result_variable[v-1].type == 'call':
                    related_func_calls.append(v)
                    funcids_remove.append(v)
                if result_variable[v-1].type == 'normal':
                    normal_funcid.append(v)
                    funcids_remove.append(v)
                if result_variable[v-1].type == 'arg':
                    arg_funcid.append(v)
                    funcids_remove.append(v)
            for i in funcids_remove:
                funcids.remove(i)

            debug_print("related funcion activation list", related_func_calls, debug_mode)
            debug_print("related normal variable list", normal_funcid, debug_mode)
            # it should be nothing left in varids
            debug_print("function ID list (final) (should be empty)", funcids, debug_mode)

            related_func_calls_add = []
            for v in related_func_calls:
                same_call = check_related_call_general(result_variable[v-1].name, result_variable[v-1].line, result_variable)
                related_func_calls_add += same_call
            for i in related_func_calls_add:
                if i not in related_func_calls:
                    related_func_calls.append(i)
            debug_print("related funcion activation list (update same_call)", related_func_calls, debug_mode)

            related_func_calls_name = []
            related_func_calls_line = []
            for i in related_func_calls:
                related_func_calls_name.append(result_variable[i-1].name)
                related_func_calls_line.append(result_variable[i-1].line)
            debug_print("related funcion activation name list", related_func_calls_name, debug_mode)
            debug_print("related funcion activation line list", related_func_calls_line, debug_mode)

            related_func_calls_id = []
            for i in range(0, len(related_func_calls_name)):
                for j in result_functionactivation:
                    if j.name == related_func_calls_name[i] and j.line == related_func_calls_line[i]:
                        related_func_calls_id.append(j.id)
            debug_print("related funcion activation ID list", related_func_calls_id, debug_mode)

            # collect the second level function calls
            for i in related_func_calls_id:
                for r in result_functionactivation:
                    if (r.caller_id == i) and r.id not in related_func_calls_id:
                        related_func_calls_id.append(r.id)
            debug_print("related funcion activation ID list (updated)", related_func_calls_id, debug_mode)

            for i in related_func_calls_id:
                for r in result_variable:
                    if r.name == result_functionactivation[i-1].name and r.line == result_functionactivation[i-1].line and r.activation_id == result_functionactivation[i-1].caller_id:
                        if r.id not in related_func_calls:
                            related_func_calls.append(r.id)
            debug_print("related funcion activation list (update second level calls)", related_func_calls, debug_mode)

            related_func_calls_add_2 = []
            for v in related_func_calls:
                same_call = check_related_call_general(result_variable[v-1].name, result_variable[v-1].line, result_variable)
                related_func_calls_add_2 += same_call
            for i in related_func_calls_add_2:
                if i not in related_func_calls:
                    related_func_calls.append(i)
            debug_print("related funcion activation list (update same_call)", related_func_calls, debug_mode)

            # double check whether we include all active functions' definition
            for v in related_func_calls:
                for r in result_variabledependency:
                    if r.source_id == v and r.type == 'direct':
                        current_line = result_variable[r.target_id-1].line
                        belong_funcdef = check_def_id(current_line, result_functiondef)
                        if belong_funcdef != 0:
                            # include all the related function definition list.
                            if belong_funcdef not in related_funcdef_list:
                                related_funcdef_list.append(belong_funcdef)
            debug_print("related function definition list (updated)", related_funcdef_list, debug_mode)

            # deal with the normal variable problem - remove irrelevant normal variable assignments
            normal_funcid.sort()
            debug_print("related normal variable list", normal_funcid, debug_mode)
            normal_funcid_remove = []
            normal_funcid_remove_add = []
            for i in normal_funcid:
                for r in result_variabledependency:
                    if r.source_id == i and r.type == "direct" and result_variable[r.target_id-1].type == 'normal':
                        if r.target_id not in normal_funcid and r.target_id not in func_params:
                            normal_funcid_remove.append(i)
                        elif r.target_id in normal_funcid_remove:
                            normal_funcid_remove.append(i)
                        elif r.target_id in normal_funcid and i in normal_funcid_remove:
                            normal_funcid_remove_add.append(i)
            debug_print("related normal variable remove list", normal_funcid_remove, debug_mode)
            for i in normal_funcid_remove:
                normal_funcid.remove(i)
            debug_print("related normal variable list (updated)", normal_funcid, debug_mode)
            for i in normal_funcid_remove_add:
                if i not in normal_funcid:
                    normal_funcid.append(i)
            for i in normal_funcid_remove_add:
                for r in result_variabledependency:
                    if r.source_id == i and r.type == "direct" and result_variable[r.target_id-1].type == 'normal':
                        if r.target_id not in normal_funcid:
                            normal_funcid.append(r.target_id)
            debug_print("related normal variable list (add back)", normal_funcid, debug_mode)

            if not more_func_name:
                for i in result_functiondef:
                    if i.name == more_func_name and i.id not in related_funcdef_list:
                        related_funcdef_list.append(i.id)
            debug_print("related function definition list (with input)", related_funcdef_list, debug_mode)

            line_list = dict()
            # add function definition
            for i in related_funcdef_list:
                tmp = result_functiondef[i-1]
                for line in range(tmp.first_line, tmp.last_line + 1):
                    if line not in line_list:
                        line_list[line] = 0

            # add function activation
            for i in related_func_calls:
                func_call_line = result_variable[i-1].line
                if func_call_line not in line_list:
                    line_list[func_call_line] = 0

            for i in graybox_funcid:
                callid = result_variable[i-1].activation_id
                act = result_functionactivation[callid-1]
                if act.caller_id == 1 and act.line not in line_list:
                    line_list[act.line] = 0

            # add normal variable
            for i in normal_funcid:
                if i in func_params:
                    func_params.remove(i)
                var_line = result_variable[i-1].line
                if var_line not in line_list:
                    line_list[var_line] = 0

            for i in arg_funcid:
                var_line = result_variable[i-1].line
                if var_line not in line_list:
                    line_list[var_line] = 0

            for i in normal_should_be_added:
                add_line = result_variable[i-1].line
                if add_line not in line_list:
                    line_list[add_line] = []
                    line_list[add_line].append(i)
                elif line_list[add_line] != 0:
                    line_list[add_line].append(i)

        # then we deal with the variable input
        else:
            # try to find the variable name when it first appears
            varid = []
            varid_copy = []
            for r in result_variable:
                if r.name == given_varname and r.activation_id == 1:
                    varid.append(r.id)
                    varid_copy.append(r.id)
            debug_print("variable ID", varid, debug_mode)

            varid_end = []
            varid_calls = []
            for v in varid_copy:
                for r in result_variabledependency:
                    if r.source_id == v and r.target_id not in varid_copy:
                        debug_detail_print(">>> target: {} <- {}, type = {}, target type = {}".format(r.source_id, r.target_id, r.type, result_variable[r.target_id-1].type), debug_mode)
                        if result_variable[r.target_id-1].type == 'function definition':
                            pass
                        elif result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                            if 'function' in result_variable[r.target_id-1].value:
                                varid_copy.append(r.target_id)
                            elif r.target_id not in varid_end:
                                varid_end.append(r.target_id)
                        elif result_variable[r.target_id-1].type == 'builtin':
                            if r.target_id not in varid_end:
                                varid_end.append(r.target_id)
                        else:
                            varid_copy.append(r.target_id)
                            if result_variable[r.target_id-1].type == 'call' and result_variable[r.target_id-1].activation_id == 1:
                                same_call = check_related_call_general(result_variable[r.target_id-1].name, result_variable[r.target_id-1].line, result_variable)
                                debug_print("call in the same line", same_call, debug_mode)
                                for i in same_call:
                                    if i not in varid_copy:
                                        debug_detail_print ("we add more calls here: {}".format(i), debug_mode)
                                        varid_copy.append(i)
                                    if i not in varid_calls:
                                        varid_calls.append(i)

            debug_print("variable ID sub list(updated target_id)", varid_copy, debug_mode)
            debug_print("variable ID end list (updated target_id)", varid_end, debug_mode)
            debug_print("variable ID related call list (updated target_id)", varid_calls, debug_mode)
            debug_print("variable ID list (updated target_id)", varid_copy, debug_mode)

            for v in varid_copy:
                for r in result_variabledependency:
                    if r.target_id == v and r.source_id not in varid_copy:
                        debug_detail_print(">>> source: {} -> {}, type = {}, source type = {}".format(r.target_id, r.source_id, r.type, result_variable[r.source_id-1].type), debug_mode)
                        if result_variable[r.source_id-1].type == '--blackbox--':
                            debug_detail_print(">>> PASS: source: {} -> {}, type = {},  source type = {}".format(r.target_id, r.source_id, r.type, result_variable[r.source_id-1].type), debug_mode)
                            pass

                        else:
                            if result_variable[r.source_id-1].type == 'normal' and result_variable[r.source_id-1].activation_id == 1:
                                varid_copy.append(r.source_id)
                                if r.source_id not in varid_end:
                                    varid_end.append(r.source_id)
                            else:
                                varid_copy.append(r.source_id)
                                if result_variable[r.source_id-1].type == 'call' and result_variable[r.source_id-1].activation_id == 1:
                                    same_call = check_related_call_general(result_variable[r.source_id-1].name, result_variable[r.source_id-1].line, result_variable)
                                    debug_print("call in the same line", same_call, debug_mode)
                                    for i in same_call:
                                        if i not in varid_copy:
                                            debug_detail_print ("we add more calls here: {}".format(i), debug_mode)
                                            varid_copy.append(i)
                                        if i not in varid_calls:
                                            varid_calls.append(i)
            debug_print("variable ID list (updated source_id)", varid_copy, debug_mode)
            debug_print("variable ID end list (updated source_id)", varid_end, debug_mode)
            debug_print("variable ID related call list (updated source_id)", varid_calls, debug_mode)

            for i in varid_calls:
                for r in result_variabledependency:
                    if r.source_id == i:
                        debug_detail_print(">>> call: {} <- {}, type = {}, target type = {}".format(r.source_id, r.target_id, r.type, result_variable[r.target_id-1].type), debug_mode)
                        if r.type == 'return' and r.target_id not in varid_calls:
                            varid_calls.append(r.target_id)
                        elif r.type == 'parameter':
                            if result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in varid_end and r.target_id not in varid_copy:
                                    varid_end.append(r.target_id)
                            elif r.target_id not in varid_calls:
                                varid_calls.append(r.target_id)
                        elif r.type == 'direct':
                            if result_variable[r.target_id-1].type == 'function definition':
                                pass
                            elif 'function' in result_variable[r.target_id-1].value and r.target_id not in varid_copy:
                                varid_copy.append(r.target_id)
                            elif result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in varid_end and r.target_id not in varid_copy:
                                    varid_end.append(r.target_id)
                            elif r.target_id not in varid_calls: 
                                varid_calls.append(r.target_id)
                        elif r.target_id not in varid_calls:
                            varid_calls.append(r.target_id)
            debug_print("variable ID related call list (updated call)", varid_calls, debug_mode)
            debug_print("variable ID end list (updated call)", varid_end, debug_mode)
            debug_print("variable ID list (updated call)", varid_copy, debug_mode)

            varids = varid_copy

            loop_list = [] # iterative variable, such as i and j
            loop_base_list = [] # variable in loop
            cond_list = []
            for v in varids:
                for r in result_variabledependency:
                    if r.source_id == v:
                        if r.type == "loop":
                            debug_detail_print(">>> loop: {} <- {}, type = {}".format(r.source_id, r.target_id, r.type), debug_mode)
                            loop_list.append(r.target_id)
                            loop_base_list.append(r.source_id)
                            if r.target_id not in varids:
                                varids.append(r.target_id)
                        if r.type == "conditional":
                            debug_detail_print(">>> cond: {} <- {}, type = {}".format(r.source_id, r.target_id, r.type), debug_mode)
                            cond_list.append(r.target_id)
                            if result_variable[r.target_id-1].type == 'normal' and result_variable[r.target_id-1].activation_id == 1:
                                if r.target_id not in varid_end and r.target_id not in varids:
                                    varid_end.append(r.target_id)
                            elif r.target_id not in varids:
                                varids.append(r.target_id)
            debug_print("variable ID (with loop and cond)", varids, debug_mode)
            debug_print("loop list", loop_list, debug_mode)
            debug_print("loop base list", loop_base_list, debug_mode)
            debug_print("cond list", cond_list, debug_mode)
            debug_print("variable end ID list", varid_end, debug_mode)

            activations = []
            for i in varid_calls:
                actid = result_variable[i-1].activation_id
                if actid > 1:
                    if actid not in activations:
                        activations.append(actid)
            debug_print("activation list", activations, debug_mode)

            graybox_varid = []
            for i in func_graybox:
                actid = result_variable[i-1].activation_id
                if actid in activations:
                    if i not in graybox_varid:
                        graybox_varid.append(i)
                    if i not in varids:
                        varids.append(i)
            debug_print("graybox variable list", graybox_varid, debug_mode)

            for v in varids:
                if result_variable[v-1].type == '--funcgraybox--':
                    graybox_varid.append(v)
            debug_print("graybox variable list", graybox_varid, debug_mode)

            # related functions' parameters
            func_params = []
            for v in graybox_varid:
                for r in result_variabledependency:
                    if r.source_id == v:
                        if r.type == "parameter":
                            if result_variable[r.target_id-1].activation_id == 1 and r.target_id not in func_params:
                                func_params.append(r.target_id)
            debug_print("var ID list (graybox updated)", varids, debug_mode)
            debug_print("function param list", func_params, debug_mode)

            func_params_remove = []
            func_params_add = []
            func_params_name = []
            for i in func_params:
                func_params_name.append(result_variable[i-1].name)

            varid_end.sort()
            debug_print("variable end ID list", varid_end, debug_mode)
            for i in varid_end:
                if result_variable[i-1].name not in func_params_name and i not in func_params_add:
                    func_params_add.append(i)
            debug_print("function param add list", func_params_add, debug_mode)

            for i in varid_end:
                for j in func_params:
                    if result_variable[i-1].name == result_variable[j-1].name and i < j:
                        if j not in func_params_remove:
                            func_params_remove.append(j)
                        if i not in func_params and i not in func_params_add:
                            func_params_add.append(i)
            debug_print("function param add list (updated)", func_params_add, debug_mode)

            func_params += func_params_add

            func_params.sort()
            debug_print("function param list (updated)", func_params, debug_mode)
            for i in func_params:
                for j in func_params:
                    # variable replication
                    if i != j and result_variable[i-1].name == result_variable[j-1].name:
                        if i < j and j not in func_params_remove:
                            func_params_remove.append(j)
            debug_print("function param remove list", func_params_remove, debug_mode)

            # remove those 'buildin' variables
            for i in func_params:
                if result_variable[i-1].type != 'normal' and i not in func_params_remove:
                    func_params_remove.append(i)
            debug_print("function param remove list (updated)", func_params_remove, debug_mode)

            normal_should_be_added = []
            for i in func_params_remove:
                func_params.remove(i) 
                if i not in varids and result_variable[i-1].type == 'normal':
                    normal_should_be_added.append(i)
            debug_print("function param list (updated)", func_params, debug_mode)
            debug_print("normal variable (should be added later)", normal_should_be_added, debug_mode)
            debug_print("var ID list (funcparams updated)", varids, debug_mode)

            params_need_trace_back = []
            for i in func_params:
                if i in loop_base_list:
                    params_need_trace_back.append(i)
            debug_print("param list (need trace back)", params_need_trace_back, debug_mode)

            for i in params_need_trace_back:
                func_params.remove(i)
                for r in result_variabledependency:
                    if r.source_id == i and r.type == 'direct' and result_variable[r.target_id-1].type == 'normal':
                        if r.target_id not in loop_list and r.target_id not in func_params:
                            func_params.append(r.target_id)
            debug_print("function param list (trace back)", func_params, debug_mode)


            related_funcdef_list = []
            varids_remove = []
            for v in varids:
                if result_variable[v-1].activation_id != 0:
                    ### this means it belongs to some functions (or loop or cond or even global functions), we will include their definitions
                    current_line = result_variable[v-1].line
                    belong_funcdef = check_def_id(current_line, result_functiondef)
                    if belong_funcdef != 0:
                        # include all the related function definition list.
                        if belong_funcdef not in related_funcdef_list:
                            related_funcdef_list.append(belong_funcdef)
                        # remove those called by some function, we will include all the function definition
                        if result_variable[v-1].activation_id != 1:
                            varids_remove.append(v)
                        # remove those function definition, we will include them 
                        if result_variable[v-1].type == 'function definition':
                            varids_remove.append(v)

            debug_print("varid remove list", varids_remove, debug_mode)
            debug_print("related function definition list", related_funcdef_list, debug_mode)

            for i in varids_remove:
                varids.remove(i)
            for i in graybox_varid:
                if i in varids:
                    varids.remove(i)
            debug_print("var ID list (updated)", varids, debug_mode)

            varids_remove = []
            related_func_calls = []
            normal_varid = []
            arg_varid = []
            for v in varids:
                if result_variable[v-1].type == 'call':
                    related_func_calls.append(v)
                    varids_remove.append(v)
                if result_variable[v-1].type == 'normal':
                    normal_varid.append(v)
                    varids_remove.append(v)
                if result_variable[v-1].type == 'arg':
                    arg_varid.append(v)
                    varids_remove.append(v)
            for i in varids_remove:
                varids.remove(i)

            debug_print("related funcion activation list", related_func_calls, debug_mode)
            debug_print("related normal variable list", normal_varid, debug_mode)
            debug_print("related arg variable list", arg_varid, debug_mode)
            # it should be nothing left in varids
            debug_print("var ID list (final)", varids, debug_mode)

            related_func_calls_add = []
            for v in related_func_calls:
                same_call = check_related_call_general(result_variable[v-1].name, result_variable[v-1].line, result_variable)
                related_func_calls_add += same_call
            for i in related_func_calls_add:
                if i not in related_func_calls:
                    related_func_calls.append(i)
            debug_print("related funcion activation list (update same_call)", related_func_calls, debug_mode)

            related_func_calls_name = []
            related_func_calls_line = []
            for i in related_func_calls:
                related_func_calls_name.append(result_variable[i-1].name)
                related_func_calls_line.append(result_variable[i-1].line)
            debug_print("related funcion activation name list", related_func_calls_name, debug_mode)
            debug_print("related funcion activation line list", related_func_calls_line, debug_mode)

            related_func_calls_id = []
            for i in range(0, len(related_func_calls_name)):
                for j in result_functionactivation:
                    if j.name == related_func_calls_name[i] and j.line == related_func_calls_line[i]:
                        related_func_calls_id.append(j.id)
            debug_print("related funcion activation ID list", related_func_calls_id, debug_mode)

            # collect the second level function calls
            for i in related_func_calls_id:
                for r in result_functionactivation:
                    if (r.caller_id == i) and r.id not in related_func_calls_id:
                        related_func_calls_id.append(r.id)
            debug_print("related funcion activation ID list (updated)", related_func_calls_id, debug_mode)

            for i in related_func_calls_id:
                for r in result_variable:
                    if r.name == result_functionactivation[i-1].name and r.line == result_functionactivation[i-1].line and r.activation_id == result_functionactivation[i-1].caller_id:
                        if r.id not in related_func_calls:
                            related_func_calls.append(r.id)
            debug_print("related funcion activation list (update second level calls)", related_func_calls, debug_mode)

            related_func_calls_add_2 = []
            for v in related_func_calls:
                same_call = check_related_call_general(result_variable[v-1].name, result_variable[v-1].line, result_variable)
                related_func_calls_add_2 += same_call
            for i in related_func_calls_add_2:
                if i not in related_func_calls:
                    related_func_calls.append(i)
            debug_print("related funcion activation list (update same_call)", related_func_calls, debug_mode)


            # double check whether we include all active functions' definition
            for v in related_func_calls:
                for r in result_variabledependency:
                    if r.source_id == v and r.type == 'direct':
                        current_line = result_variable[r.target_id-1].line
                        belong_funcdef = check_def_id(current_line, result_functiondef)
                        if belong_funcdef != 0:
                            # include all the related function definition list.
                            if belong_funcdef not in related_funcdef_list:
                                related_funcdef_list.append(belong_funcdef)
            debug_print("related function definition list (updated)", related_funcdef_list, debug_mode)

            # deal with the normal variable problem - remove irrelevant normal variable assignments
            normal_varid.sort()
            debug_print("related normal variable list", normal_varid, debug_mode)
            normal_varid_remove = []
            normal_varid_remove_add = []
            for i in normal_varid:
                for r in result_variabledependency:
                    if r.source_id == i and r.type == "direct" and result_variable[r.target_id-1].type == 'normal':
                        if r.target_id not in normal_varid and r.target_id not in func_params:
                            normal_varid_remove.append(i)
                        elif r.target_id in normal_varid_remove:
                            normal_varid_remove.append(i)
                        elif r.target_id in normal_varid and i in normal_varid_remove:
                            normal_varid_remove_add.append(i)
            debug_print("related normal variable remove list", normal_varid_remove, debug_mode)
            for i in normal_varid_remove:
                normal_varid.remove(i)
            debug_print("related normal variable list (updated)", normal_varid, debug_mode)
            debug_print("related normal variable add list (updated)", normal_varid_remove_add, debug_mode)
            for i in normal_varid_remove_add:
                if i not in normal_varid:
                    normal_varid.append(i)
            for i in normal_varid_remove_add:
                for r in result_variabledependency:
                    if r.source_id == i and r.type == "direct" and result_variable[r.target_id-1].type == 'normal':
                        if r.target_id not in normal_varid:
                            normal_varid.append(r.target_id)
            debug_print("related normal variable list (add back)", normal_varid, debug_mode)

            debug_print("undefined function name", more_func_name, debug_mode)
            debug_print("related function definition list", related_funcdef_list, debug_mode)
            if more_func_name != "":
                for i in result_functiondef:
                    if i.name == more_func_name and i.id not in related_funcdef_list:
                        related_funcdef_list.append(i.id)
            debug_print("related function definition list (with input)", related_funcdef_list, debug_mode)

            line_list = dict()
            # add function definition
            for i in related_funcdef_list:
                tmp = result_functiondef[i-1]
                for line in range(tmp.first_line, tmp.last_line + 1):
                    if line not in line_list:
                        line_list[line] = 0

            # add function activation
            for i in related_func_calls:
                func_call_line = result_variable[i-1].line
                if func_call_line not in line_list:
                    line_list[func_call_line] = 0

            for i in graybox_varid:
                callid = result_variable[i-1].activation_id
                act = result_functionactivation[callid-1]
                if act.caller_id == 1 and act.line not in line_list:
                    line_list[act.line] = 0

            # add normal variable
            for i in normal_varid:
                if i in func_params:
                    func_params.remove(i)
                var_line = result_variable[i-1].line
                if var_line not in line_list:
                    line_list[var_line] = 0

            for i in arg_varid:
                var_line = result_variable[i-1].line
                if var_line not in line_list:
                    line_list[var_line] = 0

            for i in normal_should_be_added:
                add_line = result_variable[i-1].line
                if add_line not in line_list:
                    line_list[add_line] = []
                    line_list[add_line].append(i)
                elif line_list[add_line] != 0:
                    line_list[add_line].append(i)

        debug_print("FINAL param list", func_params, debug_mode)
        param_name = []
        param_value = []
        for i in func_params:
            tmp = result_variable[i-1]
            param_name.append(tmp.name)
            param_value.append(tmp.value)

        ### function param setup
        update_file.write("\n# This is the parameter setup part\n# - We are going to setup the function parameters to make this script runnable\n# - Change the following values is useless\n")

        # This is the module part
        origin_filelines = origin_file.readlines()
        file_index = 0
        for line in origin_filelines:
            file_index = file_index + 1
            if line[0:6] == 'import' or line[0:4] == 'from':
                ### haha, this is import module part!
                update_file.write(line)

        ### write param setup to file
        for i in range(0,len(func_params)):
            string_value = str(param_value[i])
            if "array" in string_value:
                update_file.write("import numpy\n")
                string_value = "numpy." + string_value
            update_file.write(
                "{} = {}\n".format(
                    param_name[i],
                    string_value
                    )
                )

        ### copy the script
        update_file.write("\n# This is the ProvScript part\n")


        # This is the execution part
        line_keylist = line_list.keys()
        line_keylist.sort()

        ### read from original script and store content to new file
        for i in line_keylist:
            if i != 0:
                if line_list[i] == 0:    
                    content = linecache.getline(metascript.name, i)
                    content_comment = content.rstrip() + ' #####L' + str(i) + '\n'
                    update_file.write(content_comment)
                else:
                    content_comment = "# The previous script does something here, but we ignore them here\n"
                    for j in line_list[i]:
                        tmp = result_variable[j-1]
                        content_comment += "{} = {}\n".format(tmp.name, tmp.value)
                    content_comment += "# Please check the previous script\n"
                    update_file.write(content_comment)

        update_file.close()
