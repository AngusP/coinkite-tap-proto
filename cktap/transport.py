# (c) Copyright 2021 by Coinkite Inc. This file is covered by license found in COPYING-CC.
#
# transport.py
#
# Implement the desktop to card connection for our cards, both TAPSIGNER and SATSCARD.
#
#
import sys, os, cbor2
from binascii import b2a_hex, a2b_hex
from hashlib import sha256
from utils import *
from constants import *

# single-shot SHA256
sha256s = lambda msg: sha256(msg).digest()

# Correct response from all commands: 90 00 
SW_OKAY = 0x9000

# Change this to see traffic
VERBOSE = False

def find_cards():
    # search all connected card readers, and find all cards that is present
    from smartcard.System import readers as get_readers
    from smartcard.Exceptions import CardConnectionException, NoCardException

    readers = get_readers()
    if not readers:
        raise RuntimeError("Zero USB card readers found. Need at least one.")

    # search for our card
    for r in readers:
        try:
            conn = r.createConnection()
        except:
            continue
        
        try:
            conn.connect()
            atr = conn.getATR()
        except (CardConnectionException, NoCardException):
            #print(f"Empty reader: {r}")
            continue

        if atr == CARD_ATR:
            yield conn

class CKTapDeviceBase:
    # Abstract base class
    #
    def _boot(self):
        # Call this at end of __init__ to load up details from card

        st = self.send('status')
        assert 'error' not in st, 'Early failure: ' + repr(st)
        assert st['proto'] == 1, "unknown card protocol version"

        self.pubkey = st['pubkey']
        self.card_version = st['ver']
        self.birth_height = st.get('birth', None)
        self.is_testnet = st.get('testnet', False)
        assert self.card_nonce      # self.send() will have captured from first status req

        #print(f"Connected to: pubkey={B2A(self.pubkey)}")

    def __repr__(self):
        kk = b2a_hex(self.pubkey).decode('ascii')[-8:] if hasattr(self, 'pubkey') else '???'
        return '<%s: card_pubkey=...%s> ' % (__class__.__name__, kk)

    def _send_recv(self, msg):
        # do CBOR encoding and round-trip the request + response
        raise NotImplementedError

    def _nfc_read(self):
        # TODO?
        raise NotImplementedError

    def send(self, cmd, raise_on_error=True, **args):
        # Serialize command, send it as ADPU, get response and decode

        args = dict(args)
        args['cmd'] = cmd
        msg = cbor2.dumps(args)

        # Send and wait for reply
        stat_word, resp = self._send_recv(msg)

        try:
            resp = cbor2.loads(resp) if resp else {}
        except:
            print("Bad CBOR rx'd from card:\n{B2A(resp)}")
            raise pytest.fail('Bad CBOR from card')

        if stat_word != SW_OKAY:
            # Assume error if ANY bad SW value seen; promote for debug purposes
            if 'error' not in resp:
                resp['error'] = "Got error SW value: 0x%04x" % stat_word
            resp['stat_word'] = stat_word

        if VERBOSE:
            if 'error' in resp:
                print(f"Command '{cmd}' => " + ', '.join(resp.keys()))
            else:
                print(f"Command '{cmd}' => " + pformat(resp))

        if 'card_nonce' in resp:
            # many responses provide an updated card_nonce need for
            # the *next* comand. Track it
            # - only changes when "consumed" by commands that need CVC
            self.card_nonce = resp['card_nonce']

        if raise_on_error and 'error' in resp:
            msg = resp.pop('error')
            raise CardRuntimeError(msg, resp)

        return resp

    # Wrappers / helpers
    def address(self, faster=False):
        # Get current payment address for card
        # - does 100% full verification by default
        st = self.send('status')
        if 'addr' not in st:
            raise ValueError("Current slot is not yet setup.")

        n = pick_nonce()
        rr = self.send('read', nonce=n)
        
        addr = recover_address(st, rr, n)

        if not faster:
            # additional check
            my_nonce = pick_nonce()
            card_nonce = self.card_nonce
            rr = self.send('derive', nonce=my_nonce)
            master_pub = verify_master_pubkey(rr['master_pubkey'], rr['sig'],
                                                rr['chain_code'], my_nonce, card_nonce)
            derived_addr,_ = verify_derive_address(rr['chain_code'], master_pub,
                                                        testnet=self.is_testnet)
            assert derived_addr == addr

        return addr

class CKTapCard(CKTapDeviceBase):
    # talking to a real card.
    def __init__(self, card_conn=None):
        if not card_conn:
            # pick first card
            cards = list(find_cards())
            if not cards:
                raise RuntimeError("No card detected")
            card_conn = cards[0]
        else:
            # check connection they gave us
            atr = card_conn.getATR()
            assert atr == CARD_ATR, "wrong ATR from card"

        self._conn = card_conn

        # Perform "ISO Select" to pick our app
        # - 00 a4 04 00 (APPID)
        # - probably optional
        sw, resp = self._apdu(0x00, 0xa4, APP_ID, p1=4)
        assert sw == SW_OKAY, "ISO app select failed"

        self._boot()

    def _apdu(self, cls, ins, data, p1=0, p2=0):
        # send APDU to card
        lst = [ cls, ins, p1, p2, len(data)] + list(data)
        resp, sw1, sw2 = self._conn.transmit(lst)
        resp = bytes(resp)
        return ((sw1 << 8) | sw2), resp

    def _send_recv(self, msg):
        # send raw bytes (already CBOR encoded) and get response back
        assert len(msg) <= 255, "msg too long"
        return self._apdu(CBOR_CLA, CBOR_INS, msg)


# EOF
