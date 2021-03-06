#!/usr/bin/env python3
#
# Copyright (C) 2017, 2018 Jaguar Land Rover
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Authors: Shane Fagan - shane.fagan@collabora.com
#
# Authors:
#  * Gustavo Noronha <gustavo.noronha@collabora.com>
#  * Travis Reitter <travis.reitter@collabora.co.uk>
#  * Shane Fagan <shane.fagan@collabora.com>
#  * Luis Araujo <luis.araujo@collabora.co.uk>
#  * Guillaume Tucker <guillaume.tucker@collabora.com>

import os
import unittest
from subprocess import Popen, PIPE, TimeoutExpired
import vsmlib.utils
import zmq
import ipc.zeromq
import ipc.stream


RULES_PATH = os.path.abspath(os.path.join('.', 'sample_rules'))
LOGS_PATH = os.path.abspath(os.path.join('.', 'sample_logs'))
SIGNAL_NUMBER_PATH = os.path.abspath(os.path.join('.', 'signal_number_maps'))
SIGNAL_FORMAT = '{},{},\'{}\'\n'
VSM_LOG_FILE = 'vsm-tests.log'
SIGNAL_NUM_FILE = 'samples.vsi'
SIGNUM_DEFAULT = "[SIGNUM]"


def format_ipc_input(data):
    if not data:
        return []

    return [ (x.strip(), y.strip()) for x, y in \
             [ elm.split('=') for elm in data.split('\n') ] ]

def _remove_timestamp(output_string):
    # strip any prepended timestamp, if it exists
    output = ''
    for line in output_string.splitlines():
        try:
            timestamp, remainder = line.split(',', 1)
            output += remainder
        except ValueError:
            output += line

        # this re-adds a trailing newline
        output += '\n'

    return output

def _signal_format_safe(signal_to_num, signal, value):
    string = ''
    signum = None
    if signal in signal_to_num:
        signum = signal_to_num[signal]
    elif signal != '':
        signum = SIGNUM_DEFAULT

    if signum:
        string = SIGNAL_FORMAT.format(signal, signum, value)

    return string


class TestVSMDebug(object):
    module = None
    quit_command = "\nquit"

    def close(self):
        pass

    def _run_vsm(self, cmd, input_data, sig_num_path, wait_time_ms):
        data = (input_data + self.quit_command).encode('utf8')

        timeout_s = 2
        if wait_time_ms > 0:
            timeout_s = wait_time_ms / 1000

        process = Popen(cmd, stdin=PIPE, stdout=PIPE)

        try:
            output, _ = process.communicate(data, timeout_s)
        except TimeoutExpired:
            process.kill()
            return None

        cmd_output = output.decode()

        return _remove_timestamp(cmd_output)


class NoneSignalIPC(ipc.stream.StdioIPC):

    def receive(self):
        return super(ipc.stream.StdioIPC, self).receive()

    def _readline(self):
        line = super(NoneSignalIPC, self)._readline()
        if line == 'not-acceptable':
            return None
        return line


class TestVSMNoneSignal(TestVSMDebug):
    module = 'tests.NoneSignalIPC'
    quit_command = "\nquit=''"


class TestVSMZeroMQ(object):
    module = 'ipc.zeromq.ZeromqIPC'

    def __init__(self):
        self._zmq_addr = ipc.zeromq.SOCKET_ADDR
        context = zmq.Context()
        self._zmq_socket = context.socket(zmq.PAIR)
        self._zmq_socket.connect(self._zmq_addr)
        # set maximum wait on receiving (in ms)
        self._zmq_socket.RCVTIMEO = 200

    def close(self):
        self._zmq_socket.close()

    def _send(self, signal, value):
        self._zmq_socket.send_pyobj((signal, value))

    def _receive(self):
        return self._zmq_socket.recv_pyobj()

    def _receive_all(self, signal_to_num):
        process_output = ''

        # keep receiving output, one line at a time, until empty (defined as
        # a timeout of self._zmq_socket.RCVTIMEO ms -- see where that is set
        # for more information)
        while True:
            try:
                sig, val = self._receive()
                process_output += _signal_format_safe(signal_to_num, sig, val)
            except zmq.error.Again:
                # timed out on receive (which happens when we've received
                # all output)
                break

        return process_output

    def _run_vsm(self, cmd, input_data, sig_num_path, wait_time_ms):
        signal_to_num, _ = vsmlib.utils.parse_signal_num_file(sig_num_path)
        process = Popen(cmd)
        process_output = self._receive_all(signal_to_num)

        for signal, value in format_ipc_input(input_data):
            self._send(signal, value)
            # Record sent signal directly from the test.
            process_output += _signal_format_safe(signal_to_num, signal,
                                                  value)

            # fetch any pending output so send and receive output maintain
            # chronological ordering
            process_output += self._receive_all(signal_to_num)

        self._send('quit', '')
        process.wait()

        process_output += self._receive_all(signal_to_num)

        return process_output


class TestVSM(unittest.TestCase):
    ipc_class = None

    def setUp(self):
        self.ipc = self.ipc_class()

    def tearDown(self):
        self.ipc.close()

    def run_vsm(self, name, input_data, expected_output, use_initial=True,
                replay_case=None, wait_time_ms=0):
        conf = os.path.join(RULES_PATH, name + '.yaml')
        initial_state = os.path.join(RULES_PATH, name + '.initial.yaml')

        cmd = ['./vsm.py' ]

        sig_num_path = os.path.join(SIGNAL_NUMBER_PATH, SIGNAL_NUM_FILE)
        cmd += [ '--signal-number-file={}'.format(sig_num_path) ]

        # Direct verbose output (including state dumps) to log file so the tests
        # can parse them.
        cmd += [ '--log-file={}'.format(VSM_LOG_FILE) ]

        if use_initial and os.path.exists(initial_state):
            cmd += ['--initial-state={}'.format(initial_state)]

        cmd += [conf]

        if replay_case:
            replay_file = os.path.join(LOGS_PATH, replay_case + '.log')

            if os.path.exists(replay_file):
                cmd += ['--replay-log-file={}'.format(replay_file)]

        if self.ipc.module:
            cmd += ['--ipc-modules={}'.format(self.ipc.module)]

        process_output = self.ipc._run_vsm(cmd, input_data, sig_num_path,
                                           wait_time_ms)

        if process_output is None:
            self.fail("VSM process failed")

        # Read state dump from log file.
        with open(VSM_LOG_FILE) as f:
            state_output = f.read()

        log_output = _remove_timestamp(state_output)
        output_final = log_output + process_output

        self.assertEqual(output_final , expected_output)


class VSMTestCases(TestVSM):

    def test_simple0(self):
        input_data = 'transmission.gear = "reverse"'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
car.backup,3,'True'
State = {
car.backup = True
transmission.gear = reverse
}
transmission.gear,9,'"reverse"'
car.backup,3,'True'
        '''
        self.run_vsm('simple0', input_data, expected_output.strip() + '\n')

    def test_simple0_delayed(self):
        input_data = 'transmission.gear = "reverse"'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
car.backup,3,'True'
State = {
car.backup = True
transmission.gear = reverse
}
transmission.gear,9,'"reverse"'
car.backup,3,'True'
        '''
        self.run_vsm('simple0_delay', input_data, expected_output.strip() + '\n')

    def test_simple0_uninteresting(self):
        '''
        A test case where conditions to emit another signal are never triggered
        '''

        input_data = 'phone.call = "inactive"'
        expected_output = '''
phone.call,7,'inactive'
State = {
phone.call = inactive
}
condition: (phone.call == 'active') => False
phone.call,7,'"inactive"'
        '''
        self.run_vsm('simple0', input_data, expected_output.strip() + '\n')

    def test_simple2_initial(self):
        input_data = 'damage = True'
        expected_output = '''
damage,5,True
State = {
damage = True
moving = False
}
condition: (moving != True and damage == True) => True
car.stop,4,'True'
State = {
car.stop = True
damage = True
moving = False
}
damage,5,'True'
car.stop,4,'True'
        '''
        self.run_vsm('simple2', input_data, expected_output.strip() + '\n')

    def test_simple2_initial_uninteresting(self):
        '''
        A test case where conditions to emit another signal are never triggered
        '''

        input_data = 'moving = False'
        expected_output = '''
moving,6,False
State = {
moving = False
}
moving,6,'False'
        '''
        self.run_vsm('simple2', input_data, expected_output.strip() + '\n')

    def test_simple2_modify_uninteresting(self):
        '''
        A test case where conditions to emit another signal are never triggered
        '''

        input_data = 'moving = True\ndamage = True'
        expected_output = '''
moving,6,True
State = {
moving = True
}
condition: (moving != True and damage == True) => False
damage,5,True
State = {
damage = True
moving = True
}
condition: (moving != True and damage == True) => False
moving,6,'True'
damage,5,'True'
        '''
        self.run_vsm('simple2', input_data, expected_output.strip() + '\n')

    def test_simple2_multiple_signals(self):
        input_data = 'moving = False\ndamage = True'
        expected_output = '''
moving,6,False
State = {
moving = False
}
damage,5,True
State = {
damage = True
moving = False
}
condition: (moving != True and damage == True) => True
car.stop,4,'True'
State = {
car.stop = True
damage = True
moving = False
}
moving,6,'False'
damage,5,'True'
car.stop,4,'True'
        '''
        self.run_vsm('simple2', input_data, expected_output.strip() + '\n', False)

    def test_simple0_log_replay(self):
        '''
        A test of the log replay functionality
        '''

        # replay output is not currently forwarded to IPC modules
        if self.ipc.module:
            self.skipTest("test not compatible with IPC module")

        input_data = ''
        expected_output = '''
phone.call,7,'active'
State = {
phone.call = active
}
car.stop,4,'True'
State = {
car.stop = True
phone.call = active
}
phone.call,7,'active'
car.stop,4,'True'
        '''
        self.run_vsm('simple0', input_data, expected_output.strip() + '\n',
                replay_case='simple0-replay', wait_time_ms=5000)

    def test_unconditional_emit_log_replay(self):
        '''
        Regression test to ensure we don't issue duplicate unconditional emits
        when replaying.
        '''

        input_data = ''
        expected_output = '''
lock.state,13,'true'
State = {
lock.state = true
}
lock.state,13,'true'
        '''
        self.run_vsm('unconditional_emit', input_data,
                expected_output.strip() + '\n',
                replay_case='unconditional_emit', wait_time_ms=500)

    def test_simple3_xor_condition(self):
        input_data = 'phone.call = "active"\nspeed.value = 5.0'
        expected_output = '''
phone.call,7,'active'
State = {
phone.call = active
}
speed.value,8,5.0
State = {
phone.call = active
speed.value = 5.0
}
condition: (phone.call == 'active' ^^ speed.value > 50.90) => True
car.stop,4,'True'
State = {
car.stop = True
phone.call = active
speed.value = 5.0
}
phone.call,7,'"active"'
speed.value,8,'5.0'
car.stop,4,'True'
        '''
        self.run_vsm('simple3', input_data, expected_output.strip() + '\n')

    def test_monitored_condition_satisfied(self):
        '''
        This test case sets up the monitor for the subcondition and
        satisfies the subcondition before the 'stop' timeout (and thus omits the
        error message in the expected output).
        '''

        input_data = 'transmission.gear = "forward"\n' \
                'transmission.gear = "reverse"\n' \
                'camera.backup.active = True'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
transmission.gear,9,'forward'
State = {
transmission.gear = forward
}
condition: (transmission.gear == 'reverse') => False
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
lights.external.backup,14,'True'
State = {
lights.external.backup = True
transmission.gear = reverse
}
camera.backup.active,15,True
State = {
camera.backup.active = True
lights.external.backup = True
transmission.gear = reverse
}
parent condition: transmission.gear == reverse
condition: (camera.backup.active == True) => True
transmission.gear,9,'reverse'
transmission.gear,9,'"forward"'
transmission.gear,9,'"reverse"'
lights.external.backup,14,'True'
camera.backup.active,15,'True'
        '''
        self.run_vsm('monitored_condition', input_data,
                expected_output.strip() + '\n', wait_time_ms=2500)

    def test_monitored_condition_child_failure(self):
        '''
        This test case sets up the monitor for the subcondition and
        intentionally allows it to fail by not satisfying the subcondition
        before the 'stop' timeout.
        '''

        input_data = 'transmission.gear = "forward"\n' \
            'transmission.gear = "reverse"'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
transmission.gear,9,'forward'
State = {
transmission.gear = forward
}
condition: (transmission.gear == 'reverse') => False
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
lights.external.backup,14,'True'
State = {
lights.external.backup = True
transmission.gear = reverse
}
condition not met by 'start' time of 1000ms
transmission.gear,9,'reverse'
transmission.gear,9,'"forward"'
transmission.gear,9,'"reverse"'
lights.external.backup,14,'True'
        '''
        self.run_vsm('monitored_condition', input_data,
                expected_output.strip() + '\n', wait_time_ms=1500)

    def test_monitored_condition_parent_cancellation(self):
        '''
        This test case sets up the monitor for the subcondition and changes the
        evaluation of the parent condition to cancel the monitor before the
        'stop' timeout.
        '''

        input_data = 'transmission.gear = "forward"\n' \
            'transmission.gear = "reverse" \n' \
            'transmission.gear = "forward"'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
transmission.gear,9,'forward'
State = {
transmission.gear = forward
}
condition: (transmission.gear == 'reverse') => False
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
lights.external.backup,14,'True'
State = {
lights.external.backup = True
transmission.gear = reverse
}
transmission.gear,9,'forward'
State = {
lights.external.backup = True
transmission.gear = forward
}
condition: (transmission.gear == 'reverse') => False
transmission.gear,9,'reverse'
transmission.gear,9,'"forward"'
transmission.gear,9,'"reverse"'
lights.external.backup,14,'True'
transmission.gear,9,'"forward"'
        '''
        self.run_vsm('monitored_condition', input_data,
                expected_output.strip() + '\n', wait_time_ms=1500)

    def test_nested_4_condition_satisfied(self):
        '''
        This test case triggers the parent monitored condition and satisfies its
        three descendents to fully-satisfy a 4-deep nesting of conditions.
        '''

        input_data = 'a = true\n' \
                'b = true\n' \
                'c = true\n' \
                'd = true'
        expected_output = '''
a,5040,True
State = {
a = True
}
condition: (a == True) => True
b,5041,True
State = {
a = True
b = True
}
parent condition: a == True
condition: (b == True) => True
c,5042,True
State = {
a = True
b = True
c = True
}
parent condition: b == True
parent condition: a == True
condition: (c == True) => True
d,5043,True
State = {
a = True
b = True
c = True
d = True
}
parent condition: c == True
parent condition: b == True
parent condition: a == True
condition: (d == True) => True
a,5040,'true'
b,5041,'true'
c,5042,'true'
d,5043,'true'
        '''
        self.run_vsm('nested_4', input_data,
                expected_output.strip() + '\n', wait_time_ms=2200)

    def test_nested_4_condition_child_failure(self):
        '''
        This test case triggers the parent monitored condition and fails one of
        the middle conditions by the timeout.
        '''

        input_data = 'a = true\n' \
                'b = true'
        expected_output = '''
a,5040,True
State = {
a = True
}
condition: (a == True) => True
b,5041,True
State = {
a = True
b = True
}
parent condition: a == True
condition: (b == True) => True
condition not met by 'start' time of 1000ms
condition not met by 'start' time of 1500ms
a,5040,'true'
b,5041,'true'
        '''
        self.run_vsm('nested_4', input_data,
                expected_output.strip() + '\n', wait_time_ms=2200)

    def test_parallel(self):
        input_data = 'transmission.gear = "reverse"\n'\
                'wipers = True'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
reverse,16,'True'
State = {
reverse = True
transmission.gear = reverse
}
wipers,17,True
State = {
reverse = True
transmission.gear = reverse
wipers = True
}
condition: (wipers == True) => True
lights,18,'on'
State = {
lights = on
reverse = True
transmission.gear = reverse
wipers = True
}
transmission.gear,9,'"reverse"'
reverse,16,'True'
wipers,17,'True'
lights,18,'on'
        '''
        self.run_vsm('parallel', input_data, expected_output.strip() + '\n',
                False)

    def test_sequence_in_order(self):
        input_data = 'transmission.gear = "park"\n' \
                'ignition = True'
        expected_output = '''
transmission.gear,9,'park'
State = {
transmission.gear = park
}
condition: (transmission.gear == 'park') => True
parked,11,'True'
State = {
parked = True
transmission.gear = park
}
ignition,10,True
State = {
ignition = True
parked = True
transmission.gear = park
}
condition: (ignition == True) => True
ignited,12,'True'
State = {
ignited = True
ignition = True
parked = True
transmission.gear = park
}
transmission.gear,9,'"park"'
parked,11,'True'
ignition,10,'True'
ignited,12,'True'
        '''
        self.run_vsm('sequence', input_data, expected_output.strip() + '\n')

    def test_sequence_out_then_in_order(self):
        input_data = 'ignition = True\n' \
                'transmission.gear = "park"\n' \
                'ignition = True'
        expected_output = '''
ignition,10,True
State = {
ignition = True
}
changed value for signal 'ignition' ignored because prior conditions in its sequence block have not been met
transmission.gear,9,'park'
State = {
ignition = True
transmission.gear = park
}
condition: (transmission.gear == 'park') => True
parked,11,'True'
State = {
ignition = True
parked = True
transmission.gear = park
}
ignition,10,True
State = {
ignition = True
parked = True
transmission.gear = park
}
condition: (ignition == True) => True
ignited,12,'True'
State = {
ignited = True
ignition = True
parked = True
transmission.gear = park
}
ignition,10,'True'
transmission.gear,9,'"park"'
parked,11,'True'
ignition,10,'True'
ignited,12,'True'
        '''
        self.run_vsm('sequence', input_data, expected_output.strip() + '\n')

    def test_unconditional_emit(self):
        input_data = ''
        expected_output = '''
lock.state,13,'True'
State = {
lock.state = True
}
lock.state,13,'True'
        '''
        self.run_vsm('unconditional_emit', input_data,
                expected_output.strip() + '\n')

    def test_delay(self):
        input_data = 'wipers.front.on = True'
        expected_output = '''
wipers.front.on,5020,True
State = {
wipers.front.on = True
}
condition: (wipers.front.on == True) => True
lights.external.headlights,19,'True'
State = {
lights.external.headlights = True
wipers.front.on = True
}
wipers.front.on,5020,'True'
lights.external.headlights,19,'True'
        '''
        # NOTE: ideally, this would ensure the delay in output but, for
        # simplicity, that is handled in a manual test case. This simply ensures
        # the output is correct.
        self.run_vsm('delay', input_data, expected_output.strip() + '\n', False,
                wait_time_ms=2500)

    def test_subclauses_arithmetic_booleans(self):
        input_data = 'flux_capacitor.energy_generated = 1.1\nspeed.value = 140'
        expected_output = '''
flux_capacitor.energy_generated,5030,1.1
State = {
flux_capacitor.energy_generated = 1.1
}
condition: (flux_capacitor.energy_generated >= 1.21 * 0.9 and not (flux_capacitor.energy_generated >= 1.21)
) => True
lights.external.time_travel_imminent,5032,'True'
State = {
flux_capacitor.energy_generated = 1.1
lights.external.time_travel_imminent = True
}
condition: (flux_capacitor.energy_generated >= 1.21 * 0.9 and not (flux_capacitor.energy_generated >= 1.21)
) => True
lights.external.time_travel_imminent,5032,'True'
State = {
flux_capacitor.energy_generated = 1.1
lights.external.time_travel_imminent = True
}
speed.value,8,140
State = {
flux_capacitor.energy_generated = 1.1
lights.external.time_travel_imminent = True
speed.value = 140
}
condition: (( speed.value >= (88 - 10) * 1.6 and speed.value <  88 * 1.6 ) or ( flux_capacitor.energy_generated >= 1.21 * 0.9 and flux_capacitor.energy_generated < 1.21 )
) => True
lights.internal.time_travel_imminent,5031,'True'
State = {
flux_capacitor.energy_generated = 1.1
lights.external.time_travel_imminent = True
lights.internal.time_travel_imminent = True
speed.value = 140
}
condition: (( speed.value >= (88 - 10) * 1.6 and speed.value <  88 * 1.6 ) or ( flux_capacitor.energy_generated >= 1.21 * 0.9 and flux_capacitor.energy_generated < 1.21 )
) => True
lights.internal.time_travel_imminent,5031,'True'
State = {
flux_capacitor.energy_generated = 1.1
lights.external.time_travel_imminent = True
lights.internal.time_travel_imminent = True
speed.value = 140
}
flux_capacitor.energy_generated,5030,'1.1'
lights.external.time_travel_imminent,5032,'True'
lights.external.time_travel_imminent,5032,'True'
speed.value,8,'140'
lights.internal.time_travel_imminent,5031,'True'
lights.internal.time_travel_imminent,5031,'True'
        '''
        self.run_vsm('subclauses_arithmetic_booleans', input_data,
                expected_output.strip() + '\n', False)

    def test_nested_child_before_parent(self):
        '''
        Ensure that we can safely set a nested condition before its parent.

        Originally, this caused a crash.
        '''

        input_data = 'horn = true'
        expected_output = '''
horn,20,True
State = {
horn = True
}
parent condition: parked == (unset)
parent condition: car.stop == (unset)
condition: (horn == True) => True
horn,20,'true'
        '''
        self.run_vsm('nested_simple', input_data,
                expected_output.strip() + '\n', wait_time_ms=1500)

    def test_start_0_child_unmet(self):
        '''
        Ensure that we can use a start time of zero and meet its parent
        condition without crashing.
        '''

        input_data = 'parked = true'
        expected_output = '''
parked,11,True
State = {
parked = True
}
condition not met by 'start' time of 0ms
condition: (parked == True) => True
parked,11,'true'
        '''
        self.run_vsm('start_0', input_data,
                expected_output.strip() + '\n', wait_time_ms=1200)

    def test_start_0_child_met(self):
        '''
        Ensure that we can use a start time of zero and meet the full chain of
        conditions without crashing.
        '''

        input_data = 'horn = true\n' \
                'parked = true'
        expected_output = '''
horn,20,True
State = {
horn = True
}
parent condition: parked == (unset)
condition: (horn == True) => True
parked,11,True
State = {
horn = True
parked = True
}
condition: (parked == True) => True
horn,20,'true'
parked,11,'true'
        '''
        self.run_vsm('start_0', input_data,
                expected_output.strip() + '\n', wait_time_ms=1200)


class VSMStdTests(VSMTestCases):
    ipc_class = TestVSMDebug


class VSMZeroMQTests(VSMTestCases):
    ipc_class = TestVSMZeroMQ


class VSMNoneSignalTests(TestVSM):
    ipc_class = TestVSMNoneSignal

    def test_none_signal(self):
        input_data = 'transmission.gear = "reverse"\nnot-acceptable'
        expected_output = '''
transmission.gear,9,'reverse'
State = {
transmission.gear = reverse
}
condition: (transmission.gear == 'reverse') => True
car.backup,3,'True'
State = {
car.backup = True
transmission.gear = reverse
}
skipping invalid message
car.backup=True
'''
        self.run_vsm('simple0', input_data, expected_output.strip() + '\n')


if __name__ == '__main__':
    for cls in [VSMStdTests, VSMZeroMQTests, VSMNoneSignalTests]:
        suite = unittest.TestLoader().loadTestsFromTestCase(cls)
        unittest.TextTestRunner(verbosity=2).run(suite)
