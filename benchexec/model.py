# BenchExec is a framework for reliable benchmarking.
# This file is part of BenchExec.
#
# Copyright (C) 2007-2015  Dirk Beyer
# All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# prepare for Python 3
from __future__ import absolute_import, division, print_function, unicode_literals

import collections
import logging
import os
import time
import sys
import yaml
from xml.etree import ElementTree

from benchexec import BenchExecException
from benchexec import intel_cpu_energy
from benchexec import result
from benchexec import util

MEMLIMIT = "memlimit"
TIMELIMIT = "timelimit"
CORELIMIT = "cpuCores"

SOFTTIMELIMIT = "softtimelimit"
HARDTIMELIMIT = "hardtimelimit"
WALLTIMELIMIT = "walltimelimit"

PROPERTY_TAG = "propertyfile"

_BYTE_FACTOR = 1000  # byte in kilobyte

_ERROR_RESULTS_FOR_TERMINATION_REASON = {
    "memory": "OUT OF MEMORY",
    "killed": "KILLED",
    "failed": "FAILED",
    "files-count": "FILES-COUNT LIMIT",
    "files-size": "FILES-SIZE LIMIT",
}


def substitute_vars(oldList, runSet=None, task_file=None):
    """
    This method replaces special substrings from a list of string
    and return a new list.
    """
    keyValueList = []
    if runSet:
        benchmark = runSet.benchmark

        # list with tuples (key, value): 'key' is replaced by 'value'
        keyValueList = [
            ("benchmark_name", benchmark.name),
            ("benchmark_date", benchmark.instance),
            ("benchmark_path", benchmark.base_dir or "."),
            ("benchmark_path_abs", os.path.abspath(benchmark.base_dir)),
            ("benchmark_file", os.path.basename(benchmark.benchmark_file)),
            (
                "benchmark_file_abs",
                os.path.abspath(os.path.basename(benchmark.benchmark_file)),
            ),
            ("logfile_path", os.path.dirname(runSet.log_folder) or "."),
            ("logfile_path_abs", os.path.abspath(runSet.log_folder)),
            ("rundefinition_name", runSet.real_name if runSet.real_name else ""),
            ("test_name", runSet.real_name if runSet.real_name else ""),
        ]

    if task_file:
        var_prefix = "taskdef_" if task_file.endswith(".yml") else "inputfile_"
        keyValueList.append((var_prefix + "name", os.path.basename(task_file)))
        keyValueList.append((var_prefix + "path", os.path.dirname(task_file) or "."))
        keyValueList.append(
            (var_prefix + "path_abs", os.path.dirname(os.path.abspath(task_file)))
        )

    # do not use keys twice
    assert len({key for (key, value) in keyValueList}) == len(keyValueList)

    return [util.substitute_vars(s, keyValueList) for s in oldList]


def load_task_definition_file(task_def_file):
    """Open and parse a task-definition file in YAML format."""
    try:
        with open(task_def_file) as f:
            task_def = yaml.safe_load(f)
    except OSError as e:
        raise BenchExecException("Cannot open task-definition file: " + str(e))
    except yaml.YAMLError as e:
        raise BenchExecException("Invalid task definition: " + str(e))

    if str(task_def.get("format_version")) not in ["0.1", "1.0"]:
        raise BenchExecException(
            "Task-definition file {} specifies invalid format_version '{}'.".format(
                task_def_file, task_def.get("format_version")
            )
        )

    return task_def


def load_tool_info(tool_name, config):
    """
    Load the tool-info class.
    @param tool_name: The name of the tool-info module.
    Either a full Python package name or a name within the benchexec.tools package.
    @return: A tuple of the full name of the used tool-info module and an instance of the tool-info class.
    """
    tool_module = tool_name if "." in tool_name else ("benchexec.tools." + tool_name)
    try:
        if config.container:
            # lazy import because it can fail if container mode is not supported
            from benchexec import containerized_tool

            tool = containerized_tool.ContainerizedTool(tool_module, config)
        else:
            tool = __import__(tool_module, fromlist=["Tool"]).Tool()
    except ImportError as ie:
        sys.exit(
            'Unsupported tool "{0}" specified. ImportError: {1}'.format(tool_name, ie)
        )
    except AttributeError:
        sys.exit(
            'The module "{0}" does not define the necessary class "Tool", '
            "it cannot be used as tool info for BenchExec.".format(tool_module)
        )
    return tool_module, tool


def cmdline_for_run(tool, executable, options, sourcefiles, propertyfile, rlimits):
    working_directory = tool.working_directory(executable)

    def relpath(path):
        return path if os.path.isabs(path) else os.path.relpath(path, working_directory)

    rel_executable = relpath(executable)
    if os.path.sep not in rel_executable:
        rel_executable = os.path.join(os.curdir, rel_executable)
    args = tool.cmdline(
        rel_executable,
        list(options),
        list(map(relpath, sourcefiles)),
        relpath(propertyfile) if propertyfile else None,
        rlimits.copy(),
    )
    assert all(args), "Tool cmdline contains empty or None argument: " + str(args)
    args = [os.path.expandvars(arg) for arg in args]
    args = [os.path.expanduser(arg) for arg in args]
    return args


class Benchmark(object):
    """
    The class Benchmark manages the import of source files, options, columns and
    the tool from a benchmark_file.
    This class represents the <benchmark> tag.
    """

    def __init__(self, benchmark_file, config, start_time):
        """
        The constructor of Benchmark reads the source files, options, columns and the tool
        from the XML in the benchmark_file..
        """
        logging.debug("I'm loading the benchmark %s.", benchmark_file)

        self.config = config
        self.benchmark_file = benchmark_file
        self.base_dir = os.path.dirname(self.benchmark_file)

        # get benchmark-name
        self.name = os.path.basename(benchmark_file)[:-4]  # remove ending ".xml"
        if config.name:
            self.name += "." + config.name

        self.start_time = start_time
        self.instance = time.strftime("%Y-%m-%d_%H%M", self.start_time)

        self.output_base_name = config.output_path + self.name + "." + self.instance
        self.log_folder = self.output_base_name + ".logfiles" + os.path.sep
        self.log_zip = self.output_base_name + ".logfiles.zip"
        self.result_files_folder = self.output_base_name + ".files"

        # parse XML
        try:
            rootTag = ElementTree.ElementTree().parse(benchmark_file)
        except ElementTree.ParseError as e:
            sys.exit("Benchmark file {} is invalid: {}".format(benchmark_file, e))
        if "benchmark" != rootTag.tag:
            sys.exit(
                "Benchmark file {} is invalid: "
                "It's root element is not named 'benchmark'.".format(benchmark_file)
            )

        # get tool
        tool_name = rootTag.get("tool")
        if not tool_name:
            sys.exit("A tool needs to be specified in the benchmark definition file.")
        (self.tool_module, self.tool) = load_tool_info(tool_name, config)
        self.tool_name = self.tool.name()
        # will be set from the outside if necessary (may not be the case in SaaS environments)
        self.tool_version = None
        self.executable = None
        self.display_name = rootTag.get("displayName")

        def parse_memory_limit(value):
            # In a future BenchExec version, we could treat unit-less limits as bytes
            try:
                value = int(value)
            except ValueError:
                return util.parse_memory_value(value)
            else:
                raise ValueError(
                    "Memory limit must have a unit suffix, e.g., '{} MB'".format(value)
                )

        def handle_limit_value(name, key, cmdline_value, parse_fn):
            value = rootTag.get(key, None)
            # override limit from XML with values from command line
            if cmdline_value is not None:
                if cmdline_value.strip() == "-1":  # infinity
                    value = None
                else:
                    value = cmdline_value

            if value is not None:
                try:
                    self.rlimits[key] = parse_fn(value)
                except ValueError as e:
                    sys.exit("Invalid value for {} limit: {}".format(name.lower(), e))
                if self.rlimits[key] <= 0:
                    sys.exit(
                        '{} limit "{}" is invalid, it needs to be a positive number '
                        "(or -1 on the command line for disabling it).".format(
                            name, value
                        )
                    )

        self.rlimits = {}
        keys = list(rootTag.keys())
        handle_limit_value(
            "Time", TIMELIMIT, config.timelimit, util.parse_timespan_value
        )
        handle_limit_value(
            "Hard time", HARDTIMELIMIT, config.timelimit, util.parse_timespan_value
        )
        handle_limit_value(
            "Wall time", WALLTIMELIMIT, config.walltimelimit, util.parse_timespan_value
        )
        handle_limit_value("Memory", MEMLIMIT, config.memorylimit, parse_memory_limit)
        handle_limit_value("Core", CORELIMIT, config.corelimit, int)

        if HARDTIMELIMIT in self.rlimits:
            hardtimelimit = self.rlimits.pop(HARDTIMELIMIT)
            if TIMELIMIT in self.rlimits:
                if hardtimelimit < self.rlimits[TIMELIMIT]:
                    logging.warning(
                        "Hard timelimit %d is smaller than timelimit %d, ignoring the former.",
                        hardtimelimit,
                        self.rlimits[TIMELIMIT],
                    )
                elif hardtimelimit > self.rlimits[TIMELIMIT]:
                    self.rlimits[SOFTTIMELIMIT] = self.rlimits[TIMELIMIT]
                    self.rlimits[TIMELIMIT] = hardtimelimit
            else:
                self.rlimits[TIMELIMIT] = hardtimelimit

        # get number of threads, default value is 1
        self.num_of_threads = int(rootTag.get("threads")) if ("threads" in keys) else 1
        if config.num_of_threads is not None:
            self.num_of_threads = config.num_of_threads
        if self.num_of_threads < 1:
            logging.error("At least ONE thread must be given!")
            sys.exit()

        # get global options and property file
        self.options = util.get_list_from_xml(rootTag)
        self.propertyfile = util.text_or_none(
            util.get_single_child_from_xml(rootTag, PROPERTY_TAG)
        )

        # get columns
        self.columns = Benchmark.load_columns(rootTag.find("columns"))

        # get global source files, they are used in all run sets
        if rootTag.findall("sourcefiles"):
            sys.exit(
                "Benchmark file {} has unsupported old format. "
                "Rename <sourcefiles> tags to <tasks>.".format(benchmark_file)
            )
        globalSourcefilesTags = rootTag.findall("tasks")

        # get required files
        self._required_files = set()
        for required_files_tag in rootTag.findall("requiredfiles"):
            required_files = util.expand_filename_pattern(
                required_files_tag.text, self.base_dir
            )
            if not required_files:
                logging.warning(
                    "Pattern %s in requiredfiles tag did not match any file.",
                    required_files_tag.text,
                )
            self._required_files = self._required_files.union(required_files)

        # get requirements
        self.requirements = Requirements(
            rootTag.findall("require"), self.rlimits, config
        )

        result_files_tags = rootTag.findall("resultfiles")
        if result_files_tags:
            self.result_files_patterns = [
                os.path.normpath(p.text) for p in result_files_tags if p.text
            ]
            for pattern in self.result_files_patterns:
                if pattern.startswith(".."):
                    sys.exit(
                        "Invalid relative result-files pattern '{}'.".format(pattern)
                    )
        else:
            # default is "everything below current directory"
            self.result_files_patterns = ["."]

        # get benchmarks
        self.run_sets = []
        for (i, rundefinitionTag) in enumerate(rootTag.findall("rundefinition")):
            self.run_sets.append(
                RunSet(rundefinitionTag, self, i + 1, globalSourcefilesTags)
            )

        if not self.run_sets:
            logging.warning(
                "Benchmark file %s specifies no runs to execute "
                "(no <rundefinition> tags found).",
                benchmark_file,
            )

        if not any(runSet.should_be_executed() for runSet in self.run_sets):
            logging.warning(
                "No <rundefinition> tag selected, nothing will be executed."
            )
            if config.selected_run_definitions:
                logging.warning(
                    "The selection %s does not match any run definitions of %s.",
                    config.selected_run_definitions,
                    [runSet.real_name for runSet in self.run_sets],
                )
        elif config.selected_run_definitions:
            for selected in config.selected_run_definitions:
                if not any(
                    util.wildcard_match(run_set.real_name, selected)
                    for run_set in self.run_sets
                ):
                    logging.warning(
                        'The selected run definition "%s" is not present in the input file, '
                        "skipping it.",
                        selected,
                    )

    def required_files(self):
        assert self.executable is not None, "executor needs to set tool executable"
        return self._required_files.union(self.tool.program_files(self.executable))

    def working_directory(self):
        assert self.executable is not None, "executor needs to set tool executable"
        return self.tool.working_directory(self.executable)

    def environment(self):
        assert self.executable is not None, "executor needs to set tool executable"
        return self.tool.environment(self.executable)

    @staticmethod
    def load_columns(columnsTag):
        """
        @param columnsTag: the columnsTag from the XML file
        @return: a list of Columns()
        """

        logging.debug("I'm loading some columns for the outputfile.")
        columns = []
        if columnsTag is not None:  # columnsTag is optional in XML file
            for columnTag in columnsTag.findall("column"):
                pattern = columnTag.text
                title = columnTag.get("title", pattern)
                number_of_digits = columnTag.get("numberOfDigits")
                column = Column(pattern, title, number_of_digits)
                columns.append(column)
                logging.debug(
                    'Column "%s" with title "%s" loaded from XML file.',
                    column.text,
                    column.title,
                )
        return columns


class RunSet(object):
    """
    The class RunSet manages the import of files and options of a run set.
    """

    def __init__(self, rundefinitionTag, benchmark, index, globalSourcefilesTags=[]):
        """
        The constructor of RunSet reads run-set name and the source files from rundefinitionTag.
        Source files can be included or excluded, and imported from a list of
        names in another file. Wildcards and variables are expanded.
        @param rundefinitionTag: a rundefinitionTag from the XML file
        """

        self.benchmark = benchmark

        # get name of run set, name is optional, the result can be "None"
        self.real_name = rundefinitionTag.get("name")

        # index is the number of the run set
        self.index = index

        self.log_folder = benchmark.log_folder
        self.result_files_folder = benchmark.result_files_folder
        if self.real_name:
            self.log_folder += self.real_name + "."
            self.result_files_folder = os.path.join(
                self.result_files_folder, self.real_name
            )

        # get all run-set-specific options from rundefinitionTag
        self.options = benchmark.options + util.get_list_from_xml(rundefinitionTag)
        self.propertyfile = (
            util.text_or_none(
                util.get_single_child_from_xml(rundefinitionTag, PROPERTY_TAG)
            )
            or benchmark.propertyfile
        )

        # get run-set specific required files
        required_files_pattern = {
            tag.text for tag in rundefinitionTag.findall("requiredfiles")
        }

        # get all runs, a run contains one sourcefile with options
        if rundefinitionTag.findall("sourcefiles"):
            sys.exit(
                "Benchmark file {} has unsupported old format. "
                "Rename <sourcefiles> tags to <tasks>.".format(benchmark.benchmark_file)
            )
        self.blocks = self.extract_runs_from_xml(
            globalSourcefilesTags + rundefinitionTag.findall("tasks"),
            required_files_pattern,
        )
        self.runs = [run for block in self.blocks for run in block.runs]

        names = [self.real_name]
        if len(self.blocks) == 1:
            # there is exactly one source-file set to run, append its name to run-set name
            names.append(self.blocks[0].real_name)
        self.name = ".".join(filter(None, names))
        self.full_name = self.benchmark.name + (("." + self.name) if self.name else "")

        # Currently we store logfiles as "basename.log",
        # so we cannot distinguish sourcefiles in different folder with same basename.
        # For a 'local benchmark' this causes overriding of logfiles after reading them,
        # so the result is correct, only the logfile is gone.
        # For 'cloud-mode' the logfile is overridden before reading it,
        # so the result will be wrong and every measured value will be missing.
        if self.should_be_executed():
            sourcefilesSet = set()
            for run in self.runs:
                base = os.path.basename(run.identifier)
                if base in sourcefilesSet:
                    logging.warning(
                        "Input file with name '%s' appears twice in runset. "
                        "This could cause problems with equal logfile-names.",
                        base,
                    )
                else:
                    sourcefilesSet.add(base)
            del sourcefilesSet

    def should_be_executed(self):
        return not self.benchmark.config.selected_run_definitions or any(
            util.wildcard_match(self.real_name, run_definition)
            for run_definition in self.benchmark.config.selected_run_definitions
        )

    def extract_runs_from_xml(self, sourcefilesTagList, global_required_files_pattern):
        """
        This function builds a list of SourcefileSets (containing filename with options).
        The files and their options are taken from the list of sourcefilesTags.
        """
        base_dir = self.benchmark.base_dir
        # runs are structured as sourcefile sets, one set represents one sourcefiles tag
        blocks = []

        for index, sourcefilesTag in enumerate(sourcefilesTagList):
            sourcefileSetName = sourcefilesTag.get("name")
            matchName = sourcefileSetName or str(index)
            if self.benchmark.config.selected_sourcefile_sets and not any(
                util.wildcard_match(matchName, sourcefile_set)
                for sourcefile_set in self.benchmark.config.selected_sourcefile_sets
            ):
                continue

            required_files_pattern = global_required_files_pattern.union(
                {tag.text for tag in sourcefilesTag.findall("requiredfiles")}
            )

            # get lists of filenames
            task_def_files = self.get_task_def_files_from_xml(sourcefilesTag, base_dir)

            # get file-specific options for filenames
            fileOptions = util.get_list_from_xml(sourcefilesTag)
            propertyfile = util.text_or_none(
                util.get_single_child_from_xml(sourcefilesTag, PROPERTY_TAG)
            )

            # some runs need more than one sourcefile,
            # the first sourcefile is a normal 'include'-file, we use its name as identifier
            # for logfile and result-category all other files are 'append'ed.
            appendFileTags = sourcefilesTag.findall("append")

            currentRuns = []
            for identifier in task_def_files:
                if identifier.endswith(".yml"):
                    if appendFileTags:
                        raise BenchExecException(
                            "Cannot combine <append> and task-definition files in the same <tasks> tag."
                        )
                    run = self.create_run_from_task_definition(
                        identifier, fileOptions, propertyfile, required_files_pattern
                    )
                else:
                    run = self.create_run_for_input_file(
                        identifier,
                        fileOptions,
                        propertyfile,
                        required_files_pattern,
                        appendFileTags,
                    )
                if run:
                    currentRuns.append(run)

            # add runs for cases without source files
            for run in sourcefilesTag.findall("withoutfile"):
                currentRuns.append(
                    Run(
                        run.text,
                        [],
                        fileOptions,
                        self,
                        propertyfile,
                        required_files_pattern,
                    )
                )

            blocks.append(SourcefileSet(sourcefileSetName, index, currentRuns))

        if self.benchmark.config.selected_sourcefile_sets:
            for selected in self.benchmark.config.selected_sourcefile_sets:
                if not any(
                    util.wildcard_match(sourcefile_set.real_name, selected)
                    for sourcefile_set in blocks
                ):
                    logging.warning(
                        'The selected tasks "%s" are not present in the input file, '
                        "skipping them.",
                        selected,
                    )
        return blocks

    def get_task_def_files_from_xml(self, sourcefilesTag, base_dir):
        """Get the task-definition files from the XML definition. Task-definition files are files
        for which we create a run (typically an input file or a YAML task definition).
        """
        sourcefiles = []

        # get included sourcefiles
        for includedFiles in sourcefilesTag.findall("include"):
            sourcefiles += self.expand_filename_pattern(includedFiles.text, base_dir)

        # get sourcefiles from list in file
        for includesFilesFile in sourcefilesTag.findall("includesfile"):

            for file in self.expand_filename_pattern(includesFilesFile.text, base_dir):

                # check for code (if somebody confuses 'include' and 'includesfile')
                if util.is_code(file):
                    logging.error(
                        "'%s' seems to contain code instead of a set of source file names.\n"
                        "Please check your benchmark definition file "
                        "or remove bracket '{' from this file.",
                        file,
                    )
                    sys.exit()

                # read files from list
                fileWithList = open(file, "rt")
                for line in fileWithList:

                    # strip() removes 'newline' behind the line
                    line = line.strip()

                    # ignore comments and empty lines
                    if not util.is_comment(line):
                        sourcefiles += self.expand_filename_pattern(
                            line, os.path.dirname(file)
                        )

                fileWithList.close()

        # remove excluded sourcefiles
        for excludedFiles in sourcefilesTag.findall("exclude"):
            excludedFilesList = self.expand_filename_pattern(
                excludedFiles.text, base_dir
            )
            for excludedFile in excludedFilesList:
                sourcefiles = util.remove_all(sourcefiles, excludedFile)

        for excludesFilesFile in sourcefilesTag.findall("excludesfile"):
            for file in self.expand_filename_pattern(excludesFilesFile.text, base_dir):
                # read files from list
                fileWithList = open(file, "rt")
                for line in fileWithList:

                    # strip() removes 'newline' behind the line
                    line = line.strip()

                    # ignore comments and empty lines
                    if not util.is_comment(line):
                        excludedFilesList = self.expand_filename_pattern(
                            line, os.path.dirname(file)
                        )
                        for excludedFile in excludedFilesList:
                            sourcefiles = util.remove_all(sourcefiles, excludedFile)

                fileWithList.close()

        return sourcefiles

    def create_run_for_input_file(
        self,
        input_file,
        options,
        property_file,
        required_files_pattern,
        append_file_tags,
    ):
        """Create a Run from a direct definition of the main input file (without task definition)"""
        input_files = [input_file]
        base_dir = os.path.dirname(input_file)
        for append_file in append_file_tags:
            input_files.extend(
                self.expand_filename_pattern(
                    append_file.text, base_dir, sourcefile=input_file
                )
            )

        run = Run(
            input_file,
            util.get_files(input_files),  # expand directories to get their sub-files
            options,
            self,
            property_file,
            required_files_pattern,
        )

        if not run.propertyfile:
            return run

        prop = result.Property.create(run.propertyfile, allow_unknown=False)
        run.properties = [prop]
        expected_results = result.expected_results_of_file(input_file)
        if prop.name in expected_results:
            run.expected_results[prop.filename] = expected_results[prop.name]
        # We do not check here if there is an expected result for the given propertyfile
        # like we do in create_run_from_task_definition, to keep backwards compatibility.
        return run

    def create_run_from_task_definition(
        self, task_def_file, options, propertyfile, required_files_pattern
    ):
        """Create a Run from a task definition in yaml format"""
        task_def = load_task_definition_file(task_def_file)

        def expand_patterns_from_tag(tag):
            result = []
            patterns = task_def.get(tag, [])
            if isinstance(patterns, str) or not isinstance(
                patterns, collections.Iterable
            ):
                # accept single string in addition to list of strings
                patterns = [patterns]
            for pattern in patterns:
                expanded = util.expand_filename_pattern(
                    str(pattern), os.path.dirname(task_def_file)
                )
                if not expanded:
                    raise BenchExecException(
                        "Pattern '{}' in task-definition file {} did not match any paths.".format(
                            pattern, task_def_file
                        )
                    )
                expanded.sort()
                result.extend(expanded)
            return result

        input_files = expand_patterns_from_tag("input_files")
        if not input_files:
            raise BenchExecException(
                "Task-definition file {} does not define any input files.".format(
                    task_def_file
                )
            )
        required_files = expand_patterns_from_tag("required_files")

        run = Run(
            task_def_file,
            input_files,
            options,
            self,
            propertyfile,
            required_files_pattern,
            required_files,
        )

        # run.propertyfile of Run is fully determined only after Run is created,
        # thus we handle it and the expected results here.
        if not run.propertyfile:
            return run

        # TODO: support "property_name" attribute in yaml
        prop = result.Property.create(run.propertyfile, allow_unknown=True)
        run.properties = [prop]

        for prop_dict in task_def.get("properties", []):
            if not isinstance(prop_dict, dict) or "property_file" not in prop_dict:
                raise BenchExecException(
                    "Missing property file for property in task-definition file {}.".format(
                        task_def_file
                    )
                )
            expanded = util.expand_filename_pattern(
                prop_dict["property_file"], os.path.dirname(task_def_file)
            )
            if len(expanded) != 1:
                raise BenchExecException(
                    "Property pattern '{}' in task-definition file {} does not refer to exactly one file.".format(
                        prop_dict["property_file"], task_def_file
                    )
                )

            # TODO We could reduce I/O by checking absolute paths and using os.path.samestat
            # with cached stat calls.
            if prop.filename == expanded[0] or os.path.samefile(
                prop.filename, expanded[0]
            ):
                expected_result = prop_dict.get("expected_verdict")
                if expected_result is not None and not isinstance(
                    expected_result, bool
                ):
                    raise BenchExecException(
                        "Invalid expected result '{}' for property {} in task-definition file {}.".format(
                            expected_result, prop_dict["property_file"], task_def_file
                        )
                    )
                run.expected_results[prop.filename] = result.ExpectedResult(
                    expected_result, prop_dict.get("subproperty")
                )

        if not run.expected_results:
            logging.debug(
                "Ignoring run '%s' because it does not have the property from %s.",
                run.identifier,
                run.propertyfile,
            )
            return None
        elif len(run.expected_results) > 1:
            raise BenchExecException(
                "Property '{}' specified multiple times in task-definition file {}.".format(
                    prop.filename, task_def_file
                )
            )
        else:
            return run

    def expand_filename_pattern(self, pattern, base_dir, sourcefile=None):
        """
        The function expand_filename_pattern expands a filename pattern to a sorted list
        of filenames. The pattern can contain variables and wildcards.
        If base_dir is given and pattern is not absolute, base_dir and pattern are joined.
        """

        # replace vars like ${benchmark_path},
        # with converting to list and back, we can use the function 'substitute_vars()'
        expandedPattern = substitute_vars([pattern], self, sourcefile)
        assert len(expandedPattern) == 1
        expandedPattern = expandedPattern[0]

        if expandedPattern != pattern:
            logging.debug(
                "Expanded variables in expression %r to %r.", pattern, expandedPattern
            )

        fileList = util.expand_filename_pattern(expandedPattern, base_dir)

        # sort alphabetical,
        fileList.sort()

        if not fileList:
            logging.warning("No files found matching %r.", pattern)

        return fileList


class SourcefileSet(object):
    """
    A SourcefileSet contains a list of runs and a name.
    """

    def __init__(self, name, index, runs):
        self.real_name = name  # this name is optional
        self.name = name or str(index)  # this name is always non-empty
        self.runs = runs


_logged_missing_property_files = set()


class Run(object):
    """
    A Run contains some sourcefile, some options, propertyfiles and some other stuff, that is needed for the Run.
    """

    def __init__(
        self,
        identifier,
        sourcefiles,
        fileOptions,
        runSet,
        propertyfile=None,
        required_files_patterns=[],
        required_files=[],
        expected_results={},
    ):
        # identifier is used for name of logfile, substitution, result-category
        assert identifier
        self.identifier = identifier
        self.sourcefiles = sourcefiles
        self.runSet = runSet
        self.specific_options = fileOptions  # options that are specific for this run
        self.log_file = runSet.log_folder + os.path.basename(self.identifier) + ".log"
        self.result_files_folder = os.path.join(
            runSet.result_files_folder, os.path.basename(self.identifier)
        )
        self.expected_results = expected_results or {}  # filled externally

        self.required_files = set(required_files)
        rel_sourcefile = os.path.relpath(self.identifier, runSet.benchmark.base_dir)
        for pattern in required_files_patterns:
            this_required_files = runSet.expand_filename_pattern(
                pattern, runSet.benchmark.base_dir, rel_sourcefile
            )
            if not this_required_files:
                logging.warning(
                    "Pattern %s in requiredfiles tag did not match any file for task %s.",
                    pattern,
                    self.identifier,
                )
            self.required_files.update(this_required_files)

        # combine all options to be used when executing this run
        # (reduce memory-consumption: if 2 lists are equal, do not use the second one)
        self.options = runSet.options + fileOptions if fileOptions else runSet.options
        substitutedOptions = substitute_vars(self.options, runSet, self.identifier)
        if substitutedOptions != self.options:
            self.options = substitutedOptions  # for less memory again

        self.propertyfile = propertyfile or runSet.propertyfile
        self.properties = []  # filled externally

        def log_property_file_once(msg):
            if not self.propertyfile in _logged_missing_property_files:
                _logged_missing_property_files.add(self.propertyfile)
                logging.warning(msg)

        # replace run-specific stuff in the propertyfile and add it to the set of required files
        if self.propertyfile is None:
            log_property_file_once(
                "No propertyfile specified. Score computation will ignore the results."
            )
        else:
            # we check two cases: direct filename or user-defined substitution, one of them must be a 'file'
            # TODO: do we need the second case? it is equal to previous used option "-spec ${inputfile_path}/ALL.prp"
            expandedPropertyFiles = util.expand_filename_pattern(
                self.propertyfile, self.runSet.benchmark.base_dir
            )
            substitutedPropertyfiles = substitute_vars(
                [self.propertyfile], runSet, self.identifier
            )
            assert len(substitutedPropertyfiles) == 1

            if expandedPropertyFiles:
                if len(expandedPropertyFiles) > 1:
                    log_property_file_once(
                        "Pattern {0} for input file {1} in propertyfile tag matches more than one file. Only {2} will be used.".format(
                            self.propertyfile, self.identifier, expandedPropertyFiles[0]
                        )
                    )
                self.propertyfile = expandedPropertyFiles[0]
            elif substitutedPropertyfiles and os.path.isfile(
                substitutedPropertyfiles[0]
            ):
                self.propertyfile = substitutedPropertyfiles[0]
            else:
                log_property_file_once(
                    "Pattern {0} for input file {1} in propertyfile tag did not match any file. It will be ignored.".format(
                        self.propertyfile, self.identifier
                    )
                )
                self.propertyfile = None

        if self.propertyfile:
            self.required_files.add(self.propertyfile)

        self.required_files = list(self.required_files)

        # Copy columns for having own objects in run
        # (we need this for storing the results in them).
        self.columns = [
            Column(c.text, c.title, c.number_of_digits)
            for c in self.runSet.benchmark.columns
        ]

        # here we store the optional result values, e.g. memory usage, energy, host name
        # keys need to be strings, if first character is "@" the value is marked as hidden (e.g., debug info)
        self.values = {}

        # dummy values, for output in case of interrupt
        self.status = ""
        self.category = result.CATEGORY_UNKNOWN

    def cmdline(self):
        assert (
            self.runSet.benchmark.executable is not None
        ), "executor needs to set tool executable"
        return cmdline_for_run(
            self.runSet.benchmark.tool,
            self.runSet.benchmark.executable,
            self.options,
            self.sourcefiles or [self.identifier],  # identifier for <withoutfile>
            self.propertyfile,
            self.runSet.benchmark.rlimits,
        )

    def set_result(self, values, visible_columns={}):
        """Set the result of this run.
        @param values: a dictionary with result values as returned by RunExecutor.execute_run(),
            may also contain arbitrary additional values
        @param visible_columns: a set of keys of values that should be visible by default
            (i.e., not marked as hidden), apart from those that BenchExec shows by default anyway
        """
        exitcode = values.pop("exitcode", None)
        if exitcode is not None:
            if exitcode.signal:
                self.values["@exitsignal"] = exitcode.signal
            else:
                self.values["@returnvalue"] = exitcode.value

        for key, value in values.items():
            if key == "cpuenergy" and not isinstance(value, (str, bytes)):
                energy = intel_cpu_energy.format_energy_results(value)
                for energy_key, energy_value in energy.items():
                    if energy_key != "cpuenergy":
                        energy_key = "@" + energy_key
                    self.values[energy_key] = energy_value
            elif key in ["walltime", "cputime", "memory", "cpuenergy"]:
                self.values[key] = value
            elif key in visible_columns:
                self.values[key] = value
            else:
                self.values["@" + key] = value

        termination_reason = values.get("terminationreason")

        # Termination reason was not fully precise for timeouts, so we guess "timeouts"
        # if time is too high. Since removal of ulimit time limit this should not be
        # necessary, but also does not harm. We might reconsider this in the future.
        isTimeout = (
            termination_reason in ["cputime", "cputime-soft", "walltime"]
            or self._is_timeout()
        )

        # read output
        try:
            with open(self.log_file, "rt", errors="ignore") as outputFile:
                output = outputFile.readlines()
                # first 6 lines are for logging, rest is output of subprocess, see runexecutor.py for details
                output = output[6:]
        except IOError as e:
            logging.warning("Cannot read log file: %s", e.strerror)
            output = []

        self.status = self._analyze_result(
            exitcode, output, isTimeout, termination_reason
        )
        self.category = result.get_result_category(
            self.expected_results, self.status, self.properties
        )

        for column in self.columns:
            substitutedColumnText = substitute_vars(
                [column.text], self.runSet, self.sourcefiles[0]
            )[0]
            column.value = self.runSet.benchmark.tool.get_value_from_output(
                output, substitutedColumnText
            )

    def _analyze_result(self, exitcode, output, isTimeout, termination_reason):
        """Return status according to result and output of tool."""

        # Ask tool info.
        tool_status = None
        if exitcode is not None:
            logging.debug("My subprocess returned %s.", exitcode)
            tool_status = self.runSet.benchmark.tool.determine_result(
                exitcode.value or 0, exitcode.signal or 0, output, isTimeout
            )

            if tool_status in result.RESULT_LIST_OTHER:
                # for unspecific results provide some more information if possible
                if exitcode.signal == 6:
                    tool_status = "ABORTED"
                elif exitcode.signal == 11:
                    tool_status = "SEGMENTATION FAULT"
                elif exitcode.signal == 15:
                    tool_status = "KILLED"
                elif exitcode.signal:
                    tool_status = "KILLED BY SIGNAL " + str(exitcode.signal)

                elif exitcode.value:
                    tool_status = "{} ({})".format(result.RESULT_ERROR, exitcode.value)

        # Tools sometimes produce a result even after violating a resource limit.
        # This should not be counted, so we overwrite the result with TIMEOUT/OOM
        # here, if this is the case.
        # However, we don't want to forget more specific results like SEGFAULT,
        # so we do this only if the result is a "normal" one like TRUE/FALSE
        # or an unspecific one like UNKNOWN/ERROR.
        status = None
        if isTimeout:
            status = "TIMEOUT"
        elif termination_reason:
            status = _ERROR_RESULTS_FOR_TERMINATION_REASON.get(
                termination_reason, termination_reason
            )

        if not status:
            # regular termination
            status = tool_status
        elif tool_status and tool_status not in (
            result.RESULT_LIST_OTHER + [status, "KILLED", "KILLED BY SIGNAL 9"]
        ):
            # timeout/OOM but tool still returned some result
            status = "{} ({})".format(status, tool_status)

        return status

    def _is_timeout(self):
        """ try to find out whether the tool terminated because of a timeout """
        if self.values.get("cputime") is None:
            is_cpulimit = False
        else:
            rlimits = self.runSet.benchmark.rlimits
            if SOFTTIMELIMIT in rlimits:
                limit = rlimits[SOFTTIMELIMIT]
            elif TIMELIMIT in rlimits:
                limit = rlimits[TIMELIMIT]
            else:
                limit = float("inf")
            is_cpulimit = self.values["cputime"] > limit

        if self.values.get("walltime") is None:
            is_walllimit = False
        else:
            rlimits = self.runSet.benchmark.rlimits
            if WALLTIMELIMIT in rlimits:
                limit = rlimits[WALLTIMELIMIT]
            else:
                limit = float("inf")
            is_walllimit = self.values["walltime"] > limit

        return is_cpulimit or is_walllimit


class Column(object):
    """
    The class Column contains text, title and number_of_digits of a column.
    """

    def __init__(self, text, title, numOfDigits):
        self.text = text
        self.title = title
        self.number_of_digits = numOfDigits
        self.value = ""


class Requirements(object):
    """
    This class wrappes the values for the requirements.
    It parses the tags from XML to get those values.
    If no values are found, at least the limits are used as requirements.
    If the user gives a cpu_model in the config, it overrides the previous cpu_model.
    """

    def __init__(self, tags, rlimits, config):

        self.cpu_model = None
        self.memory = None
        self.cpu_cores = None

        for requireTag in tags:

            cpu_model = requireTag.get("cpuModel", None)
            if cpu_model:
                if self.cpu_model is None:
                    self.cpu_model = cpu_model
                else:
                    raise Exception("Double specification of required CPU model.")

            cpu_cores = requireTag.get("cpuCores", None)
            if cpu_cores:
                if self.cpu_cores is None:
                    if cpu_cores is not None:
                        self.cpu_cores = int(cpu_cores)
                else:
                    raise Exception("Double specification of required CPU cores.")

            memory = requireTag.get("memory", None)
            if memory:
                if self.memory is None:
                    if memory is not None:
                        try:
                            self.memory = int(memory) * _BYTE_FACTOR * _BYTE_FACTOR
                            logging.warning(
                                'Value "%s" for memory requirement interpreted as MB for backwards compatibility, '
                                "specify a unit to make this unambiguous.",
                                memory,
                            )
                        except ValueError:
                            self.memory = util.parse_memory_value(memory)
                else:
                    raise Exception("Double specification of required memory.")

        # TODO check, if we have enough requirements to reach the limits
        # TODO is this really enough? we need some overhead!
        if self.cpu_cores is None:
            self.cpu_cores = rlimits.get(CORELIMIT, None)

        if self.memory is None:
            self.memory = rlimits.get(MEMLIMIT, None)

        if hasattr(config, "cpu_model") and config.cpu_model is not None:
            # user-given model -> override value
            self.cpu_model = config.cpu_model

        if self.cpu_cores is not None and self.cpu_cores <= 0:
            raise Exception(
                "Invalid value {} for required CPU cores.".format(self.cpu_cores)
            )

        if self.memory is not None and self.memory <= 0:
            raise Exception("Invalid value {} for required memory.".format(self.memory))

    def __str__(self):
        s = ""
        if self.cpu_model:
            s += " CPU='" + self.cpu_model + "'"
        if self.cpu_cores:
            s += " Cores=" + str(self.cpu_cores)
        if self.memory:
            s += " Memory=" + str(self.memory / _BYTE_FACTOR / _BYTE_FACTOR) + " MB"

        return "Requirements:" + (s if s else " None")
