#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import socket
import threading
import time
import xmlrpclib

from PyQt4.QtGui import *
from PyQt4.QtCore import *

from electrum import bitcoin, util
from electrum import transaction
from electrum.plugins import BasePlugin, hook
from electrum.i18n import _

from electrum_gui.qt.transaction_dialog import show_transaction

import sys
import traceback


PORT = 12344
HOST = 'ecdsa.net'
server = xmlrpclib.ServerProxy('http://%s:%d'%(HOST,PORT), allow_none=True)


class Listener(util.DaemonThread):

    def __init__(self, parent):
        util.DaemonThread.__init__(self)
        self.daemon = True
        self.parent = parent
        self.received = set()
        self.keyhashes = []

    def set_keyhashes(self, keyhashes):
        self.keyhashes = keyhashes

    def clear(self, keyhash):
        server.delete(keyhash)
        self.received.remove(keyhash)

    def run(self):
        while self.running:
            if not self.keyhashes:
                time.sleep(2)
                continue
            for keyhash in self.keyhashes:
                if keyhash in self.received:
                    continue
                try:
                    message = server.get(keyhash)
                except Exception as e:
                    self.print_error("cannot contact cosigner pool")
                    time.sleep(30)
                    continue
                if message:
                    self.received.add(keyhash)
                    self.print_error("received message for", keyhash)
                    self.parent.obj.emit(SIGNAL("cosigner:receive"), keyhash,
                                         message)
            # poll every 30 seconds
            time.sleep(30)


class Plugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.listener = None
        self.obj = QObject()
        self.obj.connect(self.obj, SIGNAL('cosigner:receive'), self.on_receive)
        self.keys = []
        self.cosigner_list = []

    def on_new_window(self, window):
        self.update()

    def on_close_window(self, window):
        self.update()

    def available_wallets(self):
        result = {}
        for window in self.parent.windows:
            if window.wallet.wallet_type in ['2of2', '2of3']:
                result[window.wallet] = window
        return result

    def is_available(self):
        return bool(self.available_wallets())

    def update(self):
        wallets = self.available_wallets()
        if wallets:
            if self.listener is None:
                self.print_error("starting listener")
                self.listener = Listener(self)
                self.listener.start()
        elif self.listener:
            self.print_error("shutting down listener")
            self.listener.stop()
            self.listener = None
        self.keys = []
        self.cosigner_list = []
        for wallet, window in wallets.items():
            for key, xpub in wallet.master_public_keys.items():
                K = bitcoin.deserialize_xkey(xpub)[-1].encode('hex')
                _hash = bitcoin.Hash(K).encode('hex')
                if wallet.master_private_keys.get(key):
                    self.keys.append((key, _hash, window))
                else:
                    self.cosigner_list.append((window, xpub, K, _hash))
        if self.listener:
            self.listener.set_keyhashes([t[1] for t in self.keys])

    @hook
    def transaction_dialog(self, d):
        d.cosigner_send_button = b = QPushButton(_("Send to cosigner"))
        b.clicked.connect(lambda: self.do_send(d.tx))
        d.buttons.insert(0, b)
        self.transaction_dialog_update(d)

    @hook
    def transaction_dialog_update(self, d):
        if d.tx.is_complete() or d.wallet.can_sign(d.tx):
            d.cosigner_send_button.hide()
            return
        for window, xpub, K, _hash in self.cosigner_list:
            if window.wallet == d.wallet and self.cosigner_can_sign(d.tx, xpub):
                d.cosigner_send_button.show()
                break
        else:
            d.cosigner_send_button.hide()

    def cosigner_can_sign(self, tx, cosigner_xpub):
        from electrum.transaction import x_to_xpub
        xpub_set = set([])
        for txin in tx.inputs:
            for x_pubkey in txin['x_pubkeys']:
                xpub = x_to_xpub(x_pubkey)
                if xpub:
                    xpub_set.add(xpub)

        return cosigner_xpub in xpub_set

    def do_send(self, tx):
        for window, xpub, K, _hash in self.cosigner_list:
            if not self.cosigner_can_sign(tx, xpub):
                continue
            message = bitcoin.encrypt_message(tx.raw, K)
            try:
                server.put(_hash, message)
            except Exception as e:
                traceback.print_exc(file=sys.stdout)
                window.show_message("Failed to send transaction to cosigning pool.")
                return
            window.show_message("Your transaction was sent to the cosigning pool.\nOpen your cosigner wallet to retrieve it.")

    def on_receive(self, keyhash, message):
        self.print_error("signal arrived for", keyhash)
        for key, _hash, window in self.keys:
            if _hash == keyhash:
                break
        else:
            self.print_error("keyhash not found")
            return

        wallet = window.wallet
        if wallet.use_encryption:
            password = window.password_dialog('An encrypted transaction was retrieved from cosigning pool.\nPlease enter your password to decrypt it.')
            if not password:
                return
        else:
            password = None
            if not window.question(_("An encrypted transaction was retrieved from cosigning pool.\nDo you want to open it now?")):
                return

        xprv = wallet.get_master_private_key(key, password)
        if not xprv:
            return
        try:
            k = bitcoin.deserialize_xkey(xprv)[-1].encode('hex')
            EC = bitcoin.EC_KEY(k.decode('hex'))
            message = EC.decrypt_message(message)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            window.show_message(str(e))
            return

        self.listener.clear(keyhash)
        tx = transaction.Transaction(message)
        show_transaction(tx, window, prompt_if_unsaved=True)
