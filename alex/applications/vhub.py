#!/usr/bin/env python
# -*- coding: utf-8 -*-
import autopath

import multiprocessing
import time
import argparse
import os

from alex.components.hub import Hub
from alex.components.hub.vio import VoipIO
from alex.components.hub.vad import VAD
from alex.components.hub.asr import ASR
from alex.components.hub.slu import SLU
from alex.components.hub.dm import DM
from alex.components.hub.nlg import NLG
from alex.components.hub.tts import TTS
from alex.components.hub.messages import Command
from alex.components.hub.calldb import CallDB
from alex.utils.config import Config


class VoipHub(Hub):
    """
    VoipHub builds a full-featured VOIP telephone system.
    It builds a pipeline of ASR, SLU, DM, NLG, TTS components.
    Then it connects ASR and TTS with the VOIP to handle audio input and
    output.
    """

    def __init__(self, cfg):
        super(VoipHub, self).__init__(cfg)
        self.close_event = multiprocessing.Event()

    def write_pid_file(self, pids):
        f = open(self.cfg['VoipHub']['pid_file'], "w+")

        f.write("vhub: %d\n" % os.getpid())
        for name, pid in pids:
            f.write("%s: %d\n" % (name, pid))
        f.close()

    def run(self):
        try:
            vio_commands, vio_child_commands = multiprocessing.Pipe()  # used to send commands to VoipIO
            vio_record, vio_child_record = multiprocessing.Pipe()      # I read from this connection recorded audio
            vio_play, vio_child_play = multiprocessing.Pipe()          # I write in audio to be played

            vad_commands, vad_child_commands = multiprocessing.Pipe()   # used to send commands to VAD
            vad_audio_out, vad_child_audio_out = multiprocessing.Pipe() # used to read output audio from VAD

            asr_commands, asr_child_commands = multiprocessing.Pipe()          # used to send commands to ASR
            asr_hypotheses_out, asr_child_hypotheses = multiprocessing.Pipe()  # used to read ASR hypotheses

            slu_commands, slu_child_commands = multiprocessing.Pipe()          # used to send commands to SLU
            slu_hypotheses_out, slu_child_hypotheses = multiprocessing.Pipe()  # used to read SLU hypotheses

            dm_commands, dm_child_commands = multiprocessing.Pipe()            # used to send commands to DM
            dm_actions_out, dm_child_actions = multiprocessing.Pipe()          # used to read DM actions

            nlg_commands, nlg_child_commands = multiprocessing.Pipe()          # used to send commands to NLG
            nlg_text_out, nlg_child_text = multiprocessing.Pipe()              # used to read NLG output

            tts_commands, tts_child_commands = multiprocessing.Pipe()          # used to send commands to TTS

            command_connections = [vio_commands, vad_commands, asr_commands, slu_commands,
                                   dm_commands, nlg_commands, tts_commands]

            non_command_connections = [vio_record, vio_child_record,
                                       vio_play, vio_child_play,
                                       vad_audio_out, vad_child_audio_out,
                                       asr_hypotheses_out, asr_child_hypotheses,
                                       slu_hypotheses_out, slu_child_hypotheses,
                                       dm_actions_out, dm_child_actions,
                                       nlg_text_out, nlg_child_text]

            vio = VoipIO(self.cfg, vio_child_commands, vio_child_record, vio_child_play, self.close_event)
            vad = VAD(self.cfg, vad_child_commands, vio_record, vad_child_audio_out, self.close_event)
            asr = ASR(self.cfg, asr_child_commands, vad_audio_out, asr_child_hypotheses, self.close_event)
            slu = SLU(self.cfg, slu_child_commands, asr_hypotheses_out, slu_child_hypotheses, self.close_event)
            dm  =  DM(self.cfg,  dm_child_commands, slu_hypotheses_out, dm_child_actions, self.close_event)
            nlg = NLG(self.cfg, nlg_child_commands, dm_actions_out, nlg_child_text, self.close_event)
            tts = TTS(self.cfg, tts_child_commands, nlg_text_out, vio_play, self.close_event)

            vio.start()
            vad.start()
            asr.start()
            slu.start()
            dm.start()
            nlg.start()
            tts.start()

            self.write_pid_file([['vio', vio.pid], ['vad', vad.pid], ['asr', asr.pid],
                                 ['slu', slu.pid], ['dm', dm.pid], ['nlg', nlg.pid], ['tts', tts.pid]])

            # init the system
            call_start = 0
            call_back_time = -1
            call_back_uri = None
            number_of_turns = -1

            s_voice_activity = False
            s_last_voice_activity_time = 0
            u_voice_activity = False
            u_last_voice_activity_time = 0

            s_last_dm_activity_time = 0

            hangup = False

            call_db = CallDB(self.cfg, self.cfg['VoipHub']['call_db'], self.cfg['VoipHub']['period'])
            call_db.log()

            while 1:
                # Check the close event.
                if self.close_event.is_set():
                    return

                time.sleep(self.cfg['Hub']['main_loop_sleep_time'])

                if call_back_time != -1 and call_back_time < time.time():
                    vio_commands.send(Command('make_call(destination="%s")' % call_back_uri, 'HUB', 'VoipIO'))
                    call_back_time = -1
                    call_back_uri = None

                # read all messages
                if vio_commands.poll():
                    command = vio_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):

                        if command.parsed['__name__'] == "incoming_call":
                            self.cfg['Logging']['system_logger'].session_start(command.parsed['remote_uri'])
                            self.cfg['Logging']['system_logger'].session_system_log('config = ' + str(self.cfg))

                            self.cfg['Logging']['session_logger'].session_start(self.cfg['Logging']['system_logger'].get_session_dir_name())
                            self.cfg['Logging']['session_logger'].config('config = ' + str(self.cfg))
                            self.cfg['Logging']['session_logger'].header(self.cfg['Logging']["system_name"], self.cfg['Logging']["version"])
                            self.cfg['Logging']['session_logger'].input_source("voip")

                            self.cfg['Analytics'].start_session(command.parsed['remote_uri'])
                            self.cfg['Analytics'].track_event('vhub', 'incoming_call', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "rejected_call":
                            call_back_time = time.time() + self.cfg['VoipHub']['wait_time_before_calling_back']
                            call_back_uri = command.parsed['remote_uri']

                            self.cfg['Analytics'].track_event('vhub', 'rejected_call', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "rejected_call_from_blacklisted_uri":
                            remote_uri = command.parsed['remote_uri']

                            num_all_calls, total_time, last_period_num_calls, last_period_total_time = call_db.get_uri_stats(remote_uri)

                            m = []
                            m.append('')
                            m.append('=' * 120)
                            m.append('Rejected incoming call from blacklisted URI: %s' % remote_uri)
                            m.append('-' * 120)
                            m.append('Total calls:                  %d' % num_all_calls)
                            m.append('Total time (min):             %0.1f' % (total_time/60.0, ))
                            m.append('Last period total calls:      %d' % last_period_num_calls)
                            m.append('Last period total time (min): %0.1f' % (last_period_total_time/60.0, ))
                            m.append('=' * 120)
                            m.append('')
                            self.cfg['Logging']['system_logger'].info('\n'.join(m))

                            self.cfg['Analytics'].track_event('vhub', 'rejected_call_from_blacklisted_uri', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "call_connecting":
                            self.cfg['Analytics'].track_event('vhub', 'call_connecting', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "call_confirmed":
                            remote_uri = command.parsed['remote_uri']
                            num_all_calls, total_time, last_period_num_calls, last_period_total_time = call_db.get_uri_stats(remote_uri)

                            m = []
                            m.append('')
                            m.append('=' * 120)
                            m.append('Incoming call from :          %s' % remote_uri)
                            m.append('-' * 120)
                            m.append('Total calls:                  %d' % num_all_calls)
                            m.append('Total time (min):             %0.1f' % (total_time/60.0, ))
                            m.append('Last period total calls:      %d' % last_period_num_calls)
                            m.append('Last period total time (min): %0.1f' % (last_period_total_time/60.0, ))
                            m.append('-' * 120)

                            if last_period_num_calls > self.cfg['VoipHub']['last_period_max_num_calls'] or \
                                    last_period_total_time > self.cfg['VoipHub']['last_period_max_total_time']:
                                # prepare for ending the call
                                call_start = time.time()
                                number_of_turns = 0
                                s_voice_activity = True
                                s_last_voice_activity_time = time.time()
                                u_voice_activity = False
                                u_last_voice_activity_time = 0
                                hangup = True

                                tts_commands.send(Command('synthesize(text="%s",log="false")' % self.cfg['VoipHub']['limit_reached_message'], 'HUB', 'TTS'))
                                vio_commands.send(Command('black_list(remote_uri="%s",expire="%d")' % (remote_uri,
                                  time.time() + self.cfg['VoipHub']['blacklist_for']), 'HUB', 'VoipIO'))
                                m.append('CALL REJECTED')
                            else:
                                # init the system
                                call_start = time.time()
                                number_of_turns = 0

                                s_voice_activity = False
                                s_last_voice_activity_time = 0
                                u_voice_activity = False
                                u_last_voice_activity_time = 0
                                hungup = False

                                dm_commands.send(Command('new_dialogue()', 'HUB', 'DM'))
                                m.append('CALL ACCEPTED')

                            m.append('=' * 120)
                            m.append('')
                            self.cfg['Logging']['system_logger'].info('\n'.join(m))

                            call_db.track_confirmed_call(remote_uri)
                            self.cfg['Analytics'].track_event('vhub', 'call_confirmed', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "call_disconnected":
                            # flush vio, when flushed, vad will be flushed
                            vio_commands.send(Command('flush()', 'HUB', 'VoipIO'))

                            self.cfg['Logging']['system_logger'].session_end()
                            self.cfg['Logging']['session_logger'].session_end()

                            remote_uri = command.parsed['remote_uri']
                            call_db.track_disconnected_call(remote_uri)

                            self.cfg['Analytics'].track_event('vhub', 'call_disconnected', command.parsed['remote_uri'])

                        if command.parsed['__name__'] == "play_utterance_start":
                            s_voice_activity = True
                            s_last_voice_activity_time = time.time()

                        if command.parsed['__name__'] == "play_utterance_end":
                            s_voice_activity = False
                            s_last_voice_activity_time = time.time()

                        if command.parsed['__name__'] == "flushed":
                            # flush vad, when flushed, asr will be flushed
                            vad_commands.send(Command('flush()', 'HUB', 'VAD'))

                if vad_commands.poll():
                    command = vad_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "speech_start":
                            u_voice_activity = True

                            if s_voice_activity and s_last_voice_activity_time + 0.02 < current_time:
                                self.cfg['Analytics'].track_event('vad', 'barge_in')
                                self.cfg['Logging']['session_logger'].barge_in("system")

                                # when a user barge in into the output, all the output pipe line
                                # must be flushed
                                nlg_commands.send(Command('flush()', 'HUB', 'NLG'))
                                s_voice_activity = False
                                s_last_voice_activity_time = time.time()

                            self.cfg['Analytics'].track_event('vad', 'speech_start')

                        if command.parsed['__name__'] == "speech_end":
                            u_voice_activity = False
                            u_last_voice_activity_time = time.time()
                            self.cfg['Analytics'].track_event('vad', 'speech_stop')

                        if command.parsed['__name__'] == "flushed":
                            # flush asr, when flushed, slu will be flushed
                            asr_commands.send(Command('flush()', 'HUB', 'ASR'))

                if asr_commands.poll():
                    command = asr_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "flushed":
                            # flush slu, when flushed, dm will be flushed
                            slu_commands.send(Command('flush()', 'HUB', 'SLU'))

                if slu_commands.poll():
                    command = slu_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "flushed":
                            # flush dm, when flushed, nlg will be flushed
                            dm_commands.send(Command('flush()', 'HUB', 'DM'))
                            dm_commands.send(Command('end_dialogue()', 'HUB', 'DM'))

                if dm_commands.poll():
                    command = dm_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "hangup":
                            # prepare for ending the call
                            hangup = True
                            self.cfg['Analytics'].track_event('vhub', 'system_hangup')

                        if command.parsed['__name__'] == "dm_da_generated":
                            # record the time of the last system generated dialogue act
                            s_last_dm_activity_time = time.time()
                            number_of_turns += 1
                            self.cfg['Analytics'].track_event('dm', 'da_generated')

                        # if a dialogue act is generated, stop playing current TTS audio
                        # theoretically it is a good place because if the DM decides to be silent
                        # the TTS can continue
                        # however, if it decides to say something, it is for the implementation
                        # late to flush old audio. If implemented better, it could work.
                        # As of now, audio is flushed when speech is detected using VAD

                        if command.parsed['__name__'] == "flushed":
                            # flush nlg, when flushed, tts will be flushed
                            nlg_commands.send(Command('flush()', 'HUB', 'NLG'))

                if nlg_commands.poll():
                    command = nlg_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "flushed":
                            # flush tts, when flushed, vio will be flushed
                            tts_commands.send(Command('flush()', 'HUB', 'TTS'))

                if tts_commands.poll():
                    command = tts_commands.recv()
                    self.cfg['Logging']['system_logger'].info(command)

                    if isinstance(command, Command):
                        if command.parsed['__name__'] == "flushed":
                            # flush vio_out
                            vio_commands.send(Command('flush_out()', 'HUB', 'VIO'))

                current_time = time.time()

                if hangup and s_last_dm_activity_time + 2.0 < current_time and \
                    s_voice_activity == False and s_last_voice_activity_time + 2.0 < current_time:
                    # we are ready to hangup only when all voice activity finished,
                    hangup = False
                    vio_commands.send(Command('hangup()', 'HUB', 'VoipIO'))

                # hard hangup due to the hard limits
                if number_of_turns != -1 and current_time - call_start > self.cfg['VoipHub']['hard_time_limit'] or \
                    number_of_turns > self.cfg['VoipHub']['hard_turn_limit']:
                    number_of_turns = -1
                    vio_commands.send(Command('hangup()', 'HUB', 'VoipIO'))

            # stop processes
            vio_commands.send(Command('stop()', 'HUB', 'VoipIO'))
            vad_commands.send(Command('stop()', 'HUB', 'VAD'))
            asr_commands.send(Command('stop()', 'HUB', 'ASR'))
            slu_commands.send(Command('stop()', 'HUB', 'SLU'))
            dm_commands.send(Command('stop()', 'HUB', 'DM'))
            nlg_commands.send(Command('stop()', 'HUB', 'NLG'))
            tts_commands.send(Command('stop()', 'HUB', 'TTS'))

            # clean connections
            for c in command_connections:
                while c.poll():
                    c.recv()

            for c in non_command_connections:
                while c.poll():
                    c.recv()

            # wait for processes to stop
            vio.join()
            vad.join()
            tts.join()
        except:
            self.cfg['Logging']['system_logger'].exception('Uncaught exception in VHUB process.')
            self.close_event.set()
            raise

#########################################################################
#########################################################################


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
        VoipHub builds a full featured VOIP telephone system.
        It builds a pipeline of VAD, ASR, SLU, DM, NLG, TTS components.
        Then it connects ASR and TTS with the VOIP to handle audio input and
        output.

        The program reads the default config in the resources directory
        ('../resources/default.cfg') config in the current directory.

        In addition, it reads all config file passed as an argument of a '-c'.
        The additional config files overwrites any default or previous values.

      """)

    parser.add_argument('-c', '--configs', nargs='+',
                        help='additional configuration files')
    args = parser.parse_args()

    cfg = Config.load_configs(args.configs)

    #########################################################################
    #########################################################################
    cfg['Logging']['system_logger'].info("Voip Hub\n" + "=" * 120)

    vhub = VoipHub(cfg)

    vhub.run()
