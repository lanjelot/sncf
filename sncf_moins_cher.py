#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys
import urllib2
import re
import time
import os
from sgmllib import SGMLParser
from optparse import OptionParser
from optparse import OptionGroup
import smtplib
from email.mime.text import MIMEText
import logging
import logging.handlers
import pickle
import datetime
from datetime import datetime
import email.utils

class TrainInfo():
    def __init__(self, id, departure_time, arrival_time, price):
        self.id = id
        self.departure_time = departure_time
        self.arrival_time = arrival_time
        self.price = float(price)

        dt = lambda t: datetime.strptime(t, '%Hh%M')
        delta = dt(arrival_time) - dt(departure_time)
        self.delta_time = '%02dh%02d' % reduce(lambda x,y: divmod(x[0], y) + x[1:], [(delta.seconds,),60,60])[:2]

    def __str__(self):
        return '%(id)s: %(departure_time)s-%(arrival_time)s (%(delta_time)s) %(price).2f€' % self.__dict__

class ProposalsParser(SGMLParser):
    def reset(self):
        SGMLParser.reset(self)
        self.in_summary = False
        self.in_departure = False
        self.in_today = False
        self.proposals = {}
        self.end_of_day = False

    def start_table(self, attrs):
        strattrs = ''.join([' %s="%s"' % (key, value) for key, value in attrs])
        if re.search(r'Recapitulatif des propositions trains', strattrs):
            self.in_summary = True

    def end_table(self):
        self.in_summary = False

    def start_tr(self, attrs):
        if self.in_summary:
            if dict(attrs)['class'] == 'departureTime':
                self.in_departure = True

    def end_tr(self):
        self.in_departure = False

    def start_td(self, attrs):
        if self.in_departure:
            self.in_today = dict(attrs)['class'] in ('', 'last-row')
            if not self.in_today:
                self.end_of_day = True

    def start_a(self, attrs):
        if self.in_departure and self.in_today:
            href = dict(attrs)['href']
            parts = href.split('_')
            key = parts[-1]
            self.proposals[key] = TrainInfo(id=key, departure_time=parts[2], arrival_time=parts[3], price=parts[4])
            logger.debug('Added: %s' % self.proposals[key])

def fake_waiting(secs):
    if not opts.nopause:
        time.sleep(secs)

def parse_proposals(req):
    logger.debug('Opening: %s' % req)
    o = opener.open(req)
    p = ProposalsParser()
    p.feed(o.read())
    p.close()
    last_redirect = o.geturl()
    last_hid = re.search(r'hid=(.+)$', last_redirect).group(1)
    return p.proposals, p.end_of_day, last_hid
        
def query_proposals():
    outward_proposals = {}
    inward_proposals = {}

    search_params = {'origin_city': opts.origin_city,
                     'destination_city': opts.destination_city,
                     'outward_date': opts.outward_date,
                     'outward_time': opts.outward_time,
                     'inward_date': opts.inward_date,
                     'inward_time': opts.inward_time}

    for k, v in search_params.items(): 
        search_params[k] = v and urllib2.quote(str(v), safe='') or ''

    search_url = 'http://www.voyages-sncf.com/weblogic/expressbooking/_SvExpressBooking?' \
                 'bookingChoice=train&origin_city=%(origin_city)s&destination_city=' \
                 '%(destination_city)s&outward_date=%(outward_date)s&outward_time=' \
                 '%(outward_time)s&inward_date=%(inward_date)s&inward_time=' \
                 '%(inward_time)s&nbPassenger=1&classe=2&train=Rechercher' % search_params

    html = opener.open(search_url).read()
    outward_proposals_url = re.search(r'<a href="([^"]+)" id="url_redirect_proposals"', html, re.M).group(1)

    fake_waiting(5) # behave as a browser waiting to be redirected
    outward_proposals, end_of_day, last_hid = parse_proposals(outward_proposals_url)

    # get next trains
    while not end_of_day:
        next_proposals_url = 'http://www.voyages-sncf.com/weblogic/proposals/nextTrains?hid=%s' \
            '&rfrr=PropositionAller_body_Trains%%20suivants' % last_hid
        fake_waiting(2)
        new_proposals, end_of_day, last_hid = parse_proposals(next_proposals_url)
        outward_proposals.update(new_proposals)

    if opts.inward_date:
        post_body = '_DIALOG=&hf_help=null&hid=%s&fromProposal=true&formName=journey_0' \
                    '&UPGRADED_PREFIX_ID_JOURNEY_ID_PROPOSAL=notUpgraded_0_0' \
                    '&action%%3Abook=Valider+cet+aller' % last_hid
        referer = 'http://www.voyages-sncf.com/billet-train/resultats?hid=%s' % last_hid
        req = urllib2.Request('http://www.voyages-sncf.com/weblogic/proposals/', data=post_body, headers={'Referer': referer})
        fake_waiting(5)
        inward_proposals, end_of_day, last_hid = parse_proposals(req)

        # get next trains
        while not end_of_day:
            next_proposals_url = 'http://www.voyages-sncf.com/weblogic/proposals/nextTrains?hid=%s' \
                '&rfrr=PropositionRetour_body_Trains%%20suivants' % last_hid
            fake_waiting(2)
            new_proposals, end_of_day, last_hid = parse_proposals(next_proposals_url)
            inward_proposals.update(new_proposals)

    return (outward_proposals, inward_proposals)

def send_email(outward_report, inward_report):
    msg = ''
    if outward_report:
        msg += '\n// Aller: %s -> %s\n' % (opts.origin_city, opts.destination_city)
        msg += '\n'.join(outward_report) + '\n'
    if inward_report:
        msg += '\n// Retour: %s -> %s\n' % (opts.destination_city, opts.origin_city)
        msg += '\n'.join(inward_report)+ '\n'

    msg += '\n\nLégende'
    msg += '\n   ↑: Train devenu plus cher'
    msg += '\n   ↓: Train devenu moins cher'
    msg += '\n   N: Nouveau train proposé'
    msg += '\n   D: Train disparu des propositions'

    mtxt = MIMEText(msg, _charset='utf-8')
    mtxt['Date'] = email.utils.formatdate(localtime=True)
    mtxt['Subject'] =  '[sncf moins cher] %s - %s (aller: %s%s)' % (opts.origin_city.upper(), 
        opts.destination_city.upper(), opts.outward_date, opts.inward_date and ', retour: '+opts.inward_date or '')
    mtxt['From'] = opts.from_addr
    mtxt['To'] = ', '.join(opts.to_addr)

    if not (opts.gmail_user and opts.gmail_password):
        s = smtplib.SMTP(host='127.0.0.1', port=25)
    else:
        s = smtplib.SMTP('smtp.gmail.com', 587)
        s.ehlo()
        s.starttls()
        s.login(opts.gmail_user, opts.gmail_password)
    s.sendmail(opts.from_addr, opts.to_addr, mtxt.as_string())
    s.quit()

# proposals: {'7699': {'id': '7699', 'price': '29.5'}, 
#             '6487': {'id': '6487', 'price': '70'},
#             '3242': {'id': '3242', 'price': '13.9'}}
# proposal: {'7699': {'id': '7699', 'price': '29.5'}
def compare_proposals(old_proposals, new_proposals):
    ret_report = []
    ret_proposals = {}

    # remove proposals to ignore
    for id in opts.ignore:
        if id in new_proposals.keys():
            del new_proposals[id]

    # compare old vs new
    for new_proposal_id, new_proposal_info in sorted(new_proposals.iteritems(), key=lambda x: x[1].departure_time):
        # new proposal
        if new_proposal_id not in old_proposals.keys():
            logger.info('New proposal: %s' % new_proposal_info)
            ret_report.append('N  %s' % new_proposal_info)
        else:
            old_proposal_info = old_proposals[new_proposal_id]
            old_price = old_proposal_info.price
            new_price = new_proposal_info.price
            # price drop
            if new_price < old_price:
                details = '%s -> %.2f€ (-%.2f€)' % (old_proposal_info, new_price, old_price - new_price)
                logger.info('Price drop: '+details)
                ret_report.append('↓  %s' % details)
            # price raise
            elif new_price > old_price:
                details = '%s -> %.2f€ (+%.2f€)' % (old_proposal_info, new_price, new_price - old_price)
                logger.info('Price raise: '+details)
                ret_report.append('↑  %s' % details)
            # price match
            else:
                logger.debug('Price match: %s' % old_proposal_info)
                ret_report.append('   %s' % old_proposal_info)
        ret_proposals[new_proposal_id] = new_proposal_info
    
    # notify any removed proposal
    for old_proposal_id, old_proposal_info in old_proposals.iteritems():
        if old_proposal_id not in new_proposals.keys():
            ret_report.append('D  %s' % old_proposal_info)
    
    # check if it has become cheaper than before
    if old_proposals and new_proposals:
        min_price = lambda p: min(p.itervalues(), key=lambda x: x.price).price
        old_minprice = min_price(old_proposals)
        new_minprice = min_price(new_proposals)
        if new_minprice < old_minprice:
            ret_report.append("YES! C'est maintenant moins cher qu'avant !!")
        elif new_minprice > old_minprice:
            ret_report.append("arf... c'est redevenu plus cher")
        ret_report.append('Le moins cher: %.2f€' % new_minprice)

    return ret_proposals, ret_report

def run_loop():
    outward_proposals = {}
    inward_proposals = {}

    if opts.savefile and os.path.isfile(opts.savefile) and os.path.getsize(opts.savefile) > 0:
        fd = open(opts.savefile, 'rb')
        outward_proposals = pickle.load(fd)
        inward_proposals = pickle.load(fd)
        fd.close()

    while True:
        new_outward_proposals, new_inward_proposals = query_proposals() 
        outward_proposals, outward_report = compare_proposals(outward_proposals, new_outward_proposals)
        inward_proposals, inward_report = compare_proposals(inward_proposals, new_inward_proposals)
        
        inout_report = outward_report + inward_report
        any_change = bool(filter(lambda r: re.match(r'↑|↓|N|D', r), inout_report))
        any_cheaper = bool(filter(lambda r: re.match(r'YES|arf', r), inout_report))

        if (opts.reportall and any_change) or (not opts.reportall and any_cheaper):
            if opts.to_addr: send_email(outward_report, inward_report)

        if opts.savefile:
            fd = open(opts.savefile, 'wb')
            pickle.dump(outward_proposals, fd)
            pickle.dump(inward_proposals, fd)
            fd.close()

        if not opts.interval: return
        time.sleep(opts.interval)

def setup_logger():
    if opts.syslog and os.path.exists('/dev/log'):
        handler = logging.handlers.SysLogHandler(address='/dev/log')
        log_fmt = '%(filename)s[%(process)d]: %(levelname)-5s - %(message)s'
    else:
        handler = logging.StreamHandler(sys.stdout)
        log_fmt = '%(asctime)s %(filename)s[%(process)d]: %(levelname)-5s - %(message)s'
    handler.setFormatter(logging.Formatter(log_fmt))
    handler.setLevel(logging.DEBUG)
    
    logger = logging.getLogger('sncf_moins_cher')
    logger.setLevel(opts.debug and logging.DEBUG or logging.INFO)
    logger.addHandler(handler)
    return logger
    
def parse_options():
    usage_str = 'usage: %prog [options]\n' \
        ' $ %prog --origin-city paris --destination-city dijon --departure-date 05/03/2010 --departure-time 17\n' \
        ' $ %prog --origin-city paris --destination-city dijon --departure-date 05/03/2010 --departure-time 17 ' \
        '--return-date 07/03/2010 --return-time 16'
    parser = OptionParser(usage=usage_str)

    group1 = OptionGroup(parser, 'Required options')
    group1.add_option('--origin-city', dest='origin_city', default='paris', help='Origin city', 
        metavar='city')
    group1.add_option('--destination-city', dest='destination_city', default='dijon', help='Destination city', 
        metavar='city')
    group1.add_option('--departure-date', dest='outward_date', help='Departure date (dd/mm/yyyy)', 
        metavar='date')
    group1.add_option('--departure-time', dest='outward_time', help='Departure time (24-hour hour)', 
        metavar='hour')

    group2 = OptionGroup(parser, 'Required options for a return trip')
    group2.add_option('--return-date', dest='inward_date', help='Return date (dd/mm/yyyy)', 
        metavar='date')
    group2.add_option('--return-time', dest='inward_time', help='Return time (24-hour hour)', 
        metavar='hour')
    
    group3 = OptionGroup(parser, 'General options')
    group3.add_option('-c', '--continuous', dest='interval', type='int', help='Continuous mode. ' \
        'Repeatedly run queries every N seconds', metavar='N')
    group3.add_option('-w', '--savefile', dest='savefile', help=
        'Save proposals to file so that next run can compare new proposals with previously saved ones, ' \
        'and report any changes (price drop/raise, ...)', metavar='filepath')
    group3.add_option('-a', '--report-all', dest='reportall', action='store_true', default=False, help='Send an ' \
        'email report for any price drop/raise, or any recently added/removed proposal. Default is to only mail ' \
        'a report when the lowest fare for a single trip becomes cheaper or more expensive')
    group3.add_option('-d', '--debug', dest='debug', action='store_true', default=False, 
        help='Enable debug messages')
    group3.add_option('-s', '--syslog', dest='syslog', action='store_true', default=False, 
        help='Enable logging to syslog')
    group3.add_option('-i', '--ignore', dest='ignore', action='append', default=[], help='Train to ignore. '\
        'Use this option multiple times to ignore more trains', metavar='trainID')
    group3.add_option('-F', '--nopause', dest='nopause', action='store_true', default=False, 
        help='Do not wait several seconds between requests in order to fake genuine user browsing')

    group4 = OptionGroup(parser, 'Required options to send email alerts')
    group4.add_option('-f', '--from', dest='from_addr', help='Sender email address', metavar='email')
    group4.add_option('-t', '--to', dest='to_addr', action='append', help='Recipient email address. ' \
        'Use this option multiple times to set more recipients', metavar='email')

    group5 = OptionGroup(parser, 'Required options to use GMail SMTP server (default is to use 127.0.0.1:25)')
    group5.add_option('--gmail-user-email', dest='gmail_user', help='GMail user email address (eg. jsmith@gmail.com)',
        metavar='email')
    group5.add_option('--gmail-user-password', dest='gmail_password', help='GMail user password',
        metavar='password')

    parser.option_groups.extend([group1, group2, group3, group4, group5])
    (opts, args) = parser.parse_args()

    def check_date(*args):
      for d in args:
        if d is not None:
            datetime.strptime(d, '%d/%m/%Y')
         
    def check_time(*args):
        for h in args:
          if h is not None:
            datetime.strptime(h, '%H')

    check_date(opts.outward_date, opts.inward_date)
    check_time(opts.outward_time, opts.inward_time) 

    if not (opts.origin_city and opts.destination_city and opts.outward_date and opts.outward_time):
        parser.error('Missing required option')
    if bool(opts.inward_date) ^ bool(opts.inward_time):
        parser.error('Missing required option for a return trip')
    if bool(opts.from_addr) ^ bool(opts.to_addr):
        parser.error('Missing required option to send email alerts')
    if bool(opts.gmail_user) ^ bool(opts.gmail_password):
        parser.error('Missing required option to use GMail SMTP server')
    if opts.interval and not opts.interval > 0:
        parser.error('Seconds must be > 0')

    return opts

def init_opener():
    proxy = urllib2.ProxyHandler({}) #'http': 'http://127.0.0.1:8082', 'https': 'http://127.0.0.1:8082'})
    headers = {'User-Agent': 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)', 
               'Referer': 'http://www.voyages-sncf.com/'}
    opener = urllib2.build_opener(proxy, urllib2.HTTPCookieProcessor())
    opener.addheaders = headers.items()
    return opener

if __name__ == '__main__':
    try:
        opts = parse_options()
        logger = setup_logger()
        opener = init_opener()
        run_loop()

    except KeyboardInterrupt:
        print 'KeyboardInterrupt, exiting...'
        sys.exit(1)

# vim: ts=4 sw=4 sts=4 et
