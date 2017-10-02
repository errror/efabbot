#!/usr/bin/python3

from smtpd import SMTPServer
import asyncore
import sys, traceback

import email

import getopt
import configparser

import telepot

import soundfile
import io
import subprocess

import signal

import pprint

class EFABConfig:
    def __init__(self):
        self.getopts_short = 'hqda:p:B:r:'
        self.getopts_long  = [
            'help',
            'quiet',
            'debug',
            'smtp-address=',
            'smtp-port=',
            'telegram-bottoken=',
            'telegram-recipient=',
        ]

        self.quiet               = False
        self.debug               = False
        self.smtp_listen         = '0.0.0.0'
        self.smtp_port           = 1025
        self.telegram_bottoken   = None
        self.telegram_recipients = []
        
    def usage(self):
        print("""
efabbot.py [options] <configfile>

Options:
 --help -h               : print this help
 --quiet -q              : no output if not neccessary
 --debug -d              : more debugging output
 --smtp-address -a       : specify listen address for SMTP server
                           (default: 0.0.0.0)
 --smtp-port -p          : specify listen port for SMTP server
                           (default: 1025)
 --telegram-bottoken -B  : specify authentication token for the Telegram bot
 --telegram-recipient -r : specify Telegram recipient ids
                           (option can be used multiple times)
""")

    def parseGetopt(self, opts):
        recipients_found = False
        for o, a in opts:
            if o in ("-h", "--help"):
                self.usage()
                sys.exit()
            elif o in ("-q", "--quiet"):
                self.quiet = True
            elif o in ("-d", "--debug"):
                self.debug = True
            elif o in ("-a", "--smtp-address"):
                self.smtp_listen = a
            elif o in ("-p", "--smtp-port"):
                self.smtp_port = a
            elif o in ("-B", "--telegram-bottoken"):
                self.telegram_bottoken = a
            elif o in ("-r", "--telegram-recipient"):
                if not recipients_found:
                    self.telegram_recipients = []
                    recipients_found = True
                self.telegram_recipients.append(int(a))
            else:
                assert False, "unhandled option: %s" % o

    def parseFile(self, filename):
        cf = configparser.ConfigParser()
        cf.read(filename)
        self.quiet = cf.getboolean(
            'general', 'quiet',
            fallback=self.quiet)
        self.debug = cf.getboolean(
            'general', 'debug',
            fallback=self.debug)
        self.smtp_listen = cf.get(
            'smtpd', 'address',
            fallback=self.smtp_listen)
        self.smtp_port = cf.getint(
            'smtpd', 'port',
            fallback=self.smtp_port)
        self.telegram_bottoken = cf.get(
            'telegram', 'bottoken',
            fallback=self.telegram_bottoken)
        recipientlist = cf.get(
            'telegram', 'recipients',
            fallback='')
        self.telegram_recipients = list(
            map(
                lambda a: int(a.replace(' ', '')),
                recipientlist.split(',')
                )
            )

    def __str__(self):
        return (
            'EFABConfig(quiet=%s, debug=%s, smtp_listen=%s, smtp_port=%s, telegram_bottoken=%s, telegram_recipients=%s)' %
            (self.quiet, self.debug, self.smtp_listen, self.smtp_port, self.telegram_bottoken, self.telegram_recipients))



class Wave2Opus:
    class OpusEncError(Exception):
        pass
        
    def __init__(self, wave):
        self.wave = wave

    def asFileObject(self):
        # prepare a io.BytesIO wrapper as input for soundfile
        ulawfileobj = io.BytesIO()
        ulawfileobj.write(self.wave)
        ulawfileobj.seek(0)
        ulaw = soundfile.SoundFile(ulawfileobj)

        # and another as output for soundfile
        pcmfileobj = io.BytesIO()
        pcm = soundfile.SoundFile(
            pcmfileobj, 'w',
            channels=ulaw.channels,
            samplerate=ulaw.samplerate,
            format='WAV')
        
        # convert the ulaw input into standard pcm output
        pcm.buffer_write(ulaw.buffer_read(ctype='double'), ctype='double')

        # get the data from the output BytesIO
        pcmdata = pcmfileobj.getvalue()

        # as I did not find something better I do a callout to the opusenc
        # binary feeding it with the converted pcmdata and getting the
        # encoded opusdata from stdout
        encproc = subprocess.Popen(
            ['opusenc', '--quiet', '/dev/stdin', '/dev/stdout'],
            stdin = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.PIPE)
        opusdata, stderr = encproc.communicate(pcmdata)
        if encproc.returncode != 0:
            raise OpusEncError(stderr)

        # prepare a BytesIO as resulting file object
        opusfile = io.BytesIO()
        opusfile.write(opusdata)
        opusfile.seek(0)
        return opusfile


class EFABMailExtractor:
    def __init__(self, mailtext):
        self.text = mailtext


class EFABBot():
    def __init__(self, token, recipients, quiet = False, debug = False):
        self.token = token
        self.recipients = recipients
        self.quiet = quiet
        self.debug = debug
        self.offset = 0
        self.bot = telepot.Bot(self.token)
        self.myname = self.bot.getMe()['username']
        self.commands = {
                'start': self._handleStartCommand,
                'id':    self._handleIdCommand,
                'test':  self._handleTestCommand,
                }

    def send(self, mail):
        opus = Wave2Opus(mail.wav)
        message = EFABMailExtractor(mail.text).text
        for r in self.recipients:
            if not self.quiet:
                print(
                    'Sending to %d with caption: "%s"' %
                    (r, mail.subject))
                if self.debug:
                    print(message)
            self.bot.sendMessage(r, message)
            self.bot.sendVoice(r, opus.asFileObject(), caption=mail.subject)

    def _handleStartCommand(self, msg):
        recipient = msg['chat']['id']
        if not self.quiet:
            print('Sending /start answer to %d' % recipient)
        self.bot.sendMessage(recipient, "Ok, I'm ready.")

    def _handleTestCommand(self, msg):
        recipient = msg['chat']['id']
        if not self.quiet:
            print('Sending /test answer to %d' % recipient)
        self.bot.sendMessage(recipient, "Test bestanden.")

    def _handleIdCommand(self, msg):
        recipient = msg['chat']['id']
        if not self.quiet:
            print('Sending /id answer to %d' % recipient)
        self.bot.sendMessage(recipient, msg['chat']['id'])

    def _handleMessage(self, msg):
        if not 'text' in msg:
            return
        text = msg['text']
        for cmd, handler in self.commands.items():
            if text in [ '/'+cmd , '/'+cmd+'@'+self.myname ]:
                if self.debug:
                    print('Found command /%s, handler=%s' % (cmd, handler))
                handler(msg)

    def handleMessages(self):
        try:
            response = self.bot.getUpdates(offset=self.offset+1)
            for msg in response:
                self._handleMessage(msg['message'])
                self.offset = msg['update_id']
        except telepot.exception.BadHTTPResponse as te:
            print('Got telepot.exception.BadHTTPResponse in EFABBot.handleMessages():')
            try:
                print('response=%s' % te.response)
            except Exception as e:
                pass
            try:
                print('status=%s'   % te.status)
            except Exception as e:
                pass
            try:
                print('text=%s'     % te.text)
            except Exception as e:
                pass
        except Exception as e:
            print('Something unexptected during handleMessages(): Got Exception type %s' % type(e))
            print('Exception: %s' % e)
            traceback.print_tb(sys.exc_info()[2])


class EFABMail:
    class ParseError(Exception):
        pass

    def __init__(self, mailstring):
        mail = email.message_from_string(mailstring)
        if not mail.is_multipart():
            raise EFABMail.ParseError("mail is not MIME-multipart structured")
        payloads = mail.get_payload()
        if len(payloads) != 2:
            raise EFABMail.ParseError("mail does not contain 2 mime parts")
        if payloads[1].get_content_type() != 'audio/x-wav':
            raise EFABMail.ParseError(
                "mail does not contain a 'audio/x-wav' attachment: " %
                payloads[1].get_content_type())
        if payloads[0].get_content_type() != 'multipart/alternative':
            raise EFABMail.ParseError(
                "mail does not contain a 'multipart/alternative' body: " %
                payloads[0].get_content_type())
        body_payloads = payloads[0].get_payload()
        if body_payloads[0].get_content_type() != 'text/plain':
            raise EFABMail.ParseError(
                "mail does not contain a 'text/plain' body part: %s" %
                body_payloads[0].get_content_type())
        self.text = body_payloads[0].get_payload(decode=True).decode()
        self.wav = payloads[1].get_payload(decode=True)
        header_bytes, encoding = email.header.decode_header(mail['Subject'])[0]
        self.subject = header_bytes.decode(encoding)


class EFABServer(SMTPServer):
    def __init__(self, listen_address, listen_port, telegram_bot, quiet = False, debug = False):
        self.bot = telegram_bot
        self.quiet = quiet
        self.debug = debug
        super(EFABServer, self).__init__((listen_address, listen_port), None)

    def process_mimeparts(self, mail, indent):    
        equals = "="*80
        pprint.pprint(mail)
        if mail.is_multipart():
            for part in mail.get_payload():
                print(" "*indent+"MIME-Part: "+part.get_content_type())
                self.process_mimeparts(part, indent+2)
        else:
            print(equals)
            print(" "*indent+"Mail-Body(None-MIME): "+mail.get_content_type())
            print(mail.get_payload()[:200])
            print(equals)

    def process_message(self, peer, mailfrom, rcpttos, data):
        try:
            if not self.quiet:
                print('Message from %s: %s => %s %d Bytes' %
                        (peer[0], mailfrom, ', '.join(rcpttos), len(data)) )

            if self.debug:
                self.process_mimeparts(email.message_from_string(data), 0)

            self.bot.send(EFABMail(data))

            if not self.quiet:
                print('Telegrams sent.')

        except Exception as e:
            traceback.print_tb(sys.exc_info()[2])
            print("Something went wrong: %s" % e)

        return


if "__main__" == __name__:
    config = EFABConfig()
    opts, args = getopt.gnu_getopt(
        sys.argv[1:],
        config.getopts_short,
        config.getopts_long
        )
    if len(args) == 0:
        pass
    elif len(args) == 1:
        config.parseFile(args[0])
    else:
        config.usage()
        sys.exit(1)
    config.parseGetopt(opts)
    print(config)
    bot = EFABBot(
        config.telegram_bottoken,
        config.telegram_recipients,
        quiet=config.quiet,
        debug=config.debug)
    smtp_server = EFABServer(
        config.smtp_listen,
        config.smtp_port,
        bot,
        quiet=config.quiet,
        debug=config.debug)
    try:
        signal.signal(
            signal.SIGTERM,
            lambda s, f: (print('Exiting on TERM signal'), sys.exit(0))
            )
        while True:
            bot.handleMessages()
            asyncore.loop(timeout=1, count=1)
    except KeyboardInterrupt:
        print('Exiting on KeyboardInterrupt')
        sys.exit(0)
