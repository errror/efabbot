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

import urllib3.exceptions
import time
import json

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
        pcm.buffer_write(ulaw.buffer_read(dtype='float64'), dtype='float64')

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
        try:
            ignored_texts = [
                'Nachricht von FRITZ!Box:',
                'Die Weiterleitung der Anrufbeantworter-Nachrichten können Sie in Ihrer FRITZ!Box im Menü Telefoniegeräte deaktivieren.',
                'Die Benachrichtigung bei ankommenden Anrufen können Sie in Ihrer FRITZ!Box im Menü "System > Push Service" deaktivieren.',
            ]
            for ignore in ignored_texts:
                self.text = self.text.replace(ignore, '')
            self.text = self.text.strip()
        except Exception as e:
            print('While extracting the text, an Exception occured: %s' % e)
            print('mailtext: "%s"' % mailtext.replace('\n', '\\n'))
            self.text = mailtext


class EFABBot():
    def __init__(self, token, recipients, quiet = False, debug = False):
        self.token = token
        self.recipients = recipients
        self.quiet = quiet
        self.debug = debug
        self.offset = 0
        self.last_exception = {}
        self.exception_count = {}
        self.bot = telepot.Bot(self.token)
        self.myname = self.bot.getMe()['username']
        self.commands = {
                'start': self._handleStartCommand,
                'id':    self._handleIdCommand,
                'test':  self._handleTestCommand,
                }

    def send(self, mail):
        if mail.wav:
            opus = Wave2Opus(mail.wav)
        else:
            opus = None
        message = EFABMailExtractor(mail.text).text
        for r in self.recipients:
            if not self.quiet:
                print(
                    'Sending to %d with caption: "%s"' %
                    (r, mail.subject))
                if self.debug:
                    print(message)
            self.bot.sendMessage(r, message)
            if opus:
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

    def grace_exception(self, ex, msg):
        # error_code (telepot.exception.TelegramError) or string of exception as dict key
        try:
            code = ex.error_code
        except:
            code = str(ex)
        now = time.time()
        # when last exception is long ago
        if now - self.last_exception.get(code, 0) > 600:
            # reset exception counter
            self.exception_count[code] = 0
        # always store time of last exception
        self.last_exception[code] = now
        # increment exception counter
        self.exception_count[code] = self.exception_count.get(code, 0) + 1
        # if exception counter hits limit
        if self.exception_count[code] > 5:
            # reset it and print the message
            self.exception_count[code] = 0
            print(msg)
        # get retry_after (telepot.exception.TelegramError(Too Many Requests: retry after 5)) or use 5 seconds default
        try:
            retry_after = ex.json['parameters']['retry_after']
        except:
            retry_after = 5
        # sleep retry_after plus 1 second to ensure != 0
        time.sleep(retry_after+1)
        return
    
    def handleMessages(self):
        try:
            response = self.bot.getUpdates(offset=self.offset+1)
            for msg in response:
                if 'message' in msg:
                    self._handleMessage(msg['message'])
                self.offset = msg['update_id']
        except telepot.exception.TelegramError as te:
            # 429: Too Many Requests: retry after 5
            # 502: Bad Gateway
            if te.error_code not in [ 429, 502, ]: # never occured in 3 years
                raise te
            self.grace_exception(te, f'Got telepot.exception.TelegramError({te.description}) in EFABBot.handleMessages()')
        except urllib3.exceptions.MaxRetryError as e:
            self.grace_exception(e, 'Got urllib3.exceptions.MaxRetryError in EFABBot.handleMessages()')
        except urllib3.exceptions.ReadTimeoutError as e:
            self.grace_exception(e, 'Got urllib3.exceptions.ReadTimeoutError in EFABBot.handleMessages()')
        except urllib3.exceptions.ProtocolError as e:
            self.grace_exception(e, 'Got urllib3.exceptions.ProtocolError in EFABBot.handleMessages()')
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
        header_string, encoding = email.header.decode_header(mail['Subject'])[0]
        if encoding != None:
            #print('Got non-None encoding decoding Subject header: %s (Subject: %s)' % (encoding, header_string))
            try:
                decoded_subject = header_string.decode(encoding)
                header_string = decoded_subject
            except Exception as e:
                print('While decoding subject (%s) with encoding "%s", the following exception occured: %s' % (header_string, encoding, str(e)))
        self.subject = header_string
        payloads = mail.get_payload()
        if len(payloads) != 2:
            raise EFABMail.ParseError("mail does not contain 2 mime parts")
        if payloads[0].get_content_type() == 'multipart/alternative':
            if payloads[1].get_content_type() != 'audio/x-wav':
                raise EFABMail.ParseError(
                    "mail does not contain a 'audio/x-wav' attachment: %s" %
                    payloads[1].get_content_type())
            body_payloads = payloads[0].get_payload()
            if body_payloads[0].get_content_type() != 'text/plain':
                raise EFABMail.ParseError(
                    "mail with audio part does not contain a 'text/plain' body part: %s" %
                    body_payloads[0].get_content_type())
            self.text = body_payloads[0].get_payload(decode=True).decode()
            self.wav = payloads[1].get_payload(decode=True)
        elif payloads[0].get_content_type() == 'text/plain': 
            self.text = payloads[0].get_payload(decode=True).decode()
            self.wav = None
        else:
            raise EFABMail.ParseError(
                "mail does not contain a 'multipart/alternative' or 'text/plain' body: %s" %
                payloads[0].get_content_type())


class EFABServer(SMTPServer):
    def __init__(self, listen_address, listen_port, telegram_bot, quiet = False, debug = False):
        self.bot = telegram_bot
        self.quiet = quiet
        self.debug = debug
        super(EFABServer, self).__init__((listen_address, listen_port), None, decode_data=True)

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
                mail = email.message_from_string(data)
                print('Subject: %s' % mail['Subject'])
                self.process_mimeparts(mail, 0)

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
