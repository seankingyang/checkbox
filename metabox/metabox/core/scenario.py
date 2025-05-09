# This file is part of Checkbox.
#
# Copyright 2021 Canonical Ltd.
# Written by:
#   Maciej Kisielewski <maciej.kisielewski@canonical.com>
#   Sylvain Pineau <sylvain.pineau@canonical.com>
#
# Checkbox is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# Checkbox is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Checkbox.  If not, see <http://www.gnu.org/licenses/>.
"""
This module defines the Scenario class.

See Scenario class properties and the assert_* functions, as they serve as
the interface to a Scenario.
"""
from pathlib import Path
import re
import time
import shlex

from subprocess import CalledProcessError
from pylxd.exceptions import NotFound

from metabox.core.actions import Start, Expect, Send, SelectTestPlan
from metabox.core.aggregator import aggregator


class Scenario:
    """Definition of how to run a Checkbox session."""

    config_override = {}
    environment = {}
    launcher = None
    LAUNCHER_PATH = "/home/ubuntu/launcher.checkbox"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.name = "{}.{}".format(cls.__module__, cls.__name__)
        # If a scenario does not declare the modes it should run in,
        # assume it will run in both local and remote modes.
        if not hasattr(cls, "modes"):
            cls.modes = ["local", "remote"]
        # If a scenario does not include what version of Checkbox it should run
        # in, assume it will run in every possible ones, as defined in
        # configuration._decl_has_a_valid_origin().
        # TODO: don't hardcode it here, use shared values
        if not hasattr(cls, "origins"):
            cls.origins = ["source", "ppa", "classic-snap", "snap"]
        aggregator.add_scenario(cls)

    def __init__(
        self,
        mode,
        *releases,
        controller_revision="current",
        agent_revision="current",
    ):
        self.mode = mode
        self.releases = releases
        # machines set up by Runner.run()
        self.local_machine = None
        self.controller_machine = None
        self.controller_revision = controller_revision
        self.agent_machine = None
        self.agent_revision = agent_revision
        self.start_session = True
        self.failures = []
        self._ret_code = None
        self._stdout = ""
        self._stderr = ""
        self._outstr_full = ""
        self._pts = None

    def get_output_streams(self):
        if self._pts:
            return self._pts.stdout_data_full
        return self._outstr_full

    def run(self):
        # Simple scenarios don't need to specify a START step
        # If there's no START step, add one unless the scenario
        # explicitly says not to start a session
        if (
            not any(isinstance(s, Start) for s in self.steps)
            and self.start_session
        ):
            self.steps.insert(0, Start())
        for i, step in enumerate(self.steps):
            # Check how to start checkbox, interactively or not
            if isinstance(step, Start):
                interactive = False
                # CHECK if any EXPECT/SEND command follows
                # w/o a new call to START before it
                for next_step in self.steps[i + 1 :]:
                    if isinstance(next_step, Start):
                        break
                    if isinstance(next_step, (Expect, Send, SelectTestPlan)):
                        interactive = True
                        break
                step.kwargs["interactive"] = interactive
            try:
                # step that fail explicitly return false or raise an exception
                if not step(self):
                    self.failures.append(step)
            except (
                TimeoutError,
                ConnectionError,
                NotFound,
                CalledProcessError,
            ):
                self.failures.append(step)
                break
        if self._pts:
            self._stdout = self._pts.stdout_data_full
            # Mute the PTS since we're about to stop the machine to avoid
            # getting empty log trace events
            self._pts.verbose = False

    def _assign_outcome(self, ret_code, stdout, stderr, outstr_full):
        """Store remnants of a machine that run the scenario."""
        self._ret_code = ret_code
        self._stdout = stdout
        self._stderr = stderr
        self._outstr_full = outstr_full

    def assert_printed(self, pattern):
        """
        Check if during Checkbox execution a line produced that matches the
        pattern.
        :param patter: regular expresion to check against the lines.
        """
        regex = re.compile(pattern)
        return bool(regex.search(self._stdout)) or bool(
            regex.search(self._stderr)
        )

    def assert_not_printed(self, pattern):
        """
        Check if during Checkbox execution a line did not produced that matches
        the pattern.
        :param pattern: regular expression to check against the lines.
        """
        regex = re.compile(pattern)
        if self._pts:
            found = regex.search(self._pts.stdout_data_full)
        else:
            found = regex.search(self._stdout) or regex.search(self._stderr)
        return not found

    def assert_ret_code(self, code):
        """Check if Checkbox returned given code."""
        return code == self._ret_code

    def assertIn(self, member, container):
        return member in container

    def assertEqual(self, first, second):
        return first == second

    def assertNotEqual(self, first, second):
        return first != second

    def start(self, cmd="", interactive=False, timeout=0):
        if self.mode == "remote":
            outcome = self.start_all(interactive=interactive, timeout=timeout)
            if interactive:
                self._pts = outcome
            else:
                self._assign_outcome(*outcome)
        else:
            if self.launcher:
                cmd += " " + self.LAUNCHER_PATH
            outcome = self.local_machine.start(
                cmd=cmd,
                env=self.environment,
                interactive=interactive,
                timeout=timeout,
            )
            if interactive:
                self._pts = outcome
            else:
                self._assign_outcome(*outcome)
        return True  # return code is checked with a different operator

    def start_all(self, interactive=False, timeout=0):
        self.start_agent()
        outcome = self.start_controller(interactive, timeout)
        if interactive:
            self._pts = outcome
        else:
            self._assign_outcome(*outcome)
        return outcome

    def start_controller(self, interactive=False, timeout=0):
        outcome = self.controller_machine.start_controller(
            self.agent_machine.address,
            self.LAUNCHER_PATH,
            interactive,
            timeout=timeout,
        )
        if interactive:
            self._pts = outcome
        else:
            self._assign_outcome(*outcome)
        return outcome

    def start_agent(self, force=False):
        return self.agent_machine.start_service(force)

    def expect(self, data, timeout=60):
        assert self._pts is not None
        outcome = self._pts.expect(data, timeout)
        return outcome

    def expect_not(self, data, timeout=60):
        assert self._pts is not None
        outcome = self._pts.expect_not(data, timeout)
        return outcome

    def send(self, data):
        assert self._pts is not None
        self._pts.send(data.encode("utf-8"), binary=True)
        # send raises an exception on failure
        return True

    def sleep(self, secs):
        time.sleep(secs)
        return True

    def signal(self, signal):
        assert self._pts is not None
        self._pts.send_signal(signal)
        # same as send, this uses send under the hood
        return True

    def select_test_plan(self, testplan_id, timeout=60):
        assert self._pts is not None
        outcome = self._pts.select_test_plan(testplan_id, timeout)
        return outcome

    def check(self, result, timeout):
        if timeout < 0:
            timeout = 0
        if isinstance(result, list):
            return all(self.check(x, timeout) for x in result)
        elif isinstance(result, bool):
            return result
        return result.check(timeout)

    def run_cmd(
        self,
        cmd,
        env={},
        interactive=False,
        timeout=0,
        target="all",
        check=True,
    ):
        # interactive mode run_cmd is non-interactive, therefore we may need
        # to wait till deadline to fetch the result
        deadline = time.time() + timeout
        if self.mode == "remote":
            if target == "controller":
                result = self.controller_machine.run_cmd(
                    cmd, env, interactive, timeout
                )
            elif target == "agent":
                result = self.agent_machine.run_cmd(
                    cmd, env, interactive, timeout
                )
            else:
                result = [
                    self.controller_machine.run_cmd(
                        cmd, env, interactive, timeout
                    ),
                    self.agent_machine.run_cmd(cmd, env, interactive, timeout),
                ]
        else:
            result = self.local_machine.run_cmd(cmd, env, interactive, timeout)

        if check:
            return self.check(result, time.time() - deadline)
        else:
            return result

    def reboot(self, timeout=0, target="all"):
        if self.mode == "remote":
            if target == "controller":
                self.controller_machine.reboot(timeout)
            elif target == "agent":
                self.agent_machine.reboot(timeout)
            else:
                self.controller_machine.reboot(timeout)
                self.agent_machine.reboot(timeout)
        else:
            self.local_machine.reboot(timeout)
        return True

    def put(self, filepath, data, mode=None, uid=1000, gid=1000, target="all"):
        if self.mode == "remote":
            if target == "controller":
                self.controller_machine.put(filepath, data, mode, uid, gid)
            elif target == "agent":
                self.agent_machine.put(filepath, data, mode, uid, gid)
            else:
                self.controller_machine.put(filepath, data, mode, uid, gid)
                self.agent_machine.put(filepath, data, mode, uid, gid)
        else:
            self.local_machine.put(filepath, data, mode, uid, gid)
        # put raises an exception on failure
        return True

    def switch_on_networking(self, target="all"):
        if self.mode == "remote":
            if target == "controller":
                self.controller_machine.switch_on_networking()
            elif target == "agent":
                self.agent_machine.switch_on_networking()
            else:
                self.controller_machine.switch_on_networking()
                self.agent_machine.switch_on_networking()
        else:
            self.local_machine.switch_on_networking()
        return True

    def switch_off_networking(self, target="all"):
        if self.mode == "remote":
            if target == "controller":
                self.controller_machine.switch_off_networking()
            elif target == "agent":
                self.agent_machine.switch_off_networking()
            else:
                self.controller_machine.switch_off_networking()
                self.agent_machine.switch_off_networking()
        else:
            self.local_machine.switch_off_networking()
        return True

    def stop_agent(self):
        return self.agent_machine.stop_agent()

    def reboot_agent(self):
        return self.agent_machine.reboot_agent()

    def is_agent_active(self):
        return self.agent_machine.is_agent_active()

    def mktree(
        self, path, privileged=False, timeout=0, target="all", check=False
    ):
        """
        Creates a directory including any missing parent
        """
        cmd = ["mkdir", "-p", path]
        if privileged:
            cmd = ["sudo"] + cmd
        cmd_str = shlex.join(cmd)
        return self.run_cmd(
            cmd_str, target=target, timeout=timeout, check=check
        )

    def run_manage(self, args, timeout=0, target="all"):
        """
        Runs the manage.py script with some arguments
        """
        path = "/home/ubuntu/checkbox/metabox/metabox/metabox-provider"
        cmd = f"bash -c 'cd {path} ; python3 manage.py {args}'"
        return self.run_cmd(cmd, target=target, timeout=timeout)

    def assert_in_file(self, pattern, path):
        """
        Check if a file created during Checkbox execution contains text that
        matches the pattern.
        :param patter: regular expresion to check against the lines.
        :param path: path to the file
        """
        if isinstance(path, Path):
            path = str(path)

        result = self.run_cmd(f"cat {path}", check=False)
        regex = re.compile(pattern)
        return bool(regex.search(result.stdout))
