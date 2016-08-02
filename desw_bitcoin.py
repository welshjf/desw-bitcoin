"""
Plugin for bitcoind over RPC.
This module can be imported by desw and used like a plugin.
It also is meant to be called from the command line.
To configure for use with bitcoind, call this file from
walletnotify and blocknotify.
"""

import threading
import signal
import Queue
import json
import grp
import os
from pycoin.key.validate import is_address_valid
from desw import CFG, models, ses, logger, process_credit, confirm_send

from bitcoinrpc.authproxy import AuthServiceProxy
NETCODES = ['BTC', 'XTN']
NETWORK = 'Bitcoin'
CURRENCIES = json.loads(CFG.get(NETWORK.lower(), 'CURRENCIES'))
CONFS = int(CFG.get(NETWORK.lower(), 'CONFS'))
FEE = int(CFG.get(NETWORK.lower(), 'FEE'))


def create_client():
    """
    Create an RPC client.

    :rtype: AuthServiceProxy
    """
    return AuthServiceProxy(CFG.get(NETWORK.lower(), 'RPCURL'))


def get_new_address():
    """
    Get a new address from the client.

    :rtype: str
    """
    client = create_client()
    return str(client.getnewaddress())


def validate_address(address, network=None):
    """
    Validate an address of the given network.

    :param str address: The address to validate
    :param str network: The network the address belongs to (i.e. BTC)
    :rtype: bool
    """

    try:
        netcode = is_address_valid(address, allowable_netcodes=NETCODES)
    except Exception:
        return False
    if netcode is None or (network is not None and netcode != network):
        return False
    return True


def send_to_address(address, amount):
    """
    Send the amount of coins to the address indicated.

    :param str address: The address to send to
    :param float amount: The amount of coins to send as a float
    :return: the transaction id (txid)
    :rtype: str
    """
    client = create_client()
    txid = str(client.sendtoaddress(address, amount))
    adjust_hwbalance(available=-amount, total=-amount)
    return txid


def get_balance():
    """
    Get the wallet's balance. Returns a dict with 'available' and 'total'
    balances, indicating what can be spent right now, and what is the total
    including unconfirmed funds.

    :rtype: dict
    """
    hwb = ses.query(models.HWBalance).filter(models.HWBalance.network == NETWORK.lower()).order_by(models.HWBalance.time.desc()).first()
    return {'total': hwb.total, 'available': hwb.available}


def process_receive(txid, details, confirmed=False):
    """
    Process an incoming transaction with the given txid and details.
    If valid and new, create a Credit and update the corresponding Balance.

    :param str txid: The txid for the transaction in question
    :param dict details: The transaction details as returned by rpc client.
    :param bool confirmed: Has this transaction received enough confirmations?
    """
    creds = ses.query(models.Credit).filter(models.Credit.ref_id == txid)
    if creds.count() > 0:
        logger.info("txid already known. returning.")
        return
    state = 'complete' if confirmed else 'unconfirmed'
    addy = ses.query(models.Address)\
        .filter(models.Address.address == details['address']).first()
    if not addy:
        logger.warning("address not known. returning.")
        return
    amount = int(float(details['amount']) * 1e8)
    logger.info("crediting txid %s" % txid)
    process_credit(amount=amount, address=details['address'],
                   currency=CURRENCIES[0], network=NETWORK, state=state,
                   reference='tx received', ref_id=txid,
                   user_id=addy.user_id)
    adjust_hwbalance(available=None, total=amount)


def adjust_hwbalance(available=None, total=None):
    if available is None and total is None:
        return
    hwb = ses.query(models.HWBalance).filter(models.HWBalance.network == NETWORK.lower()).order_by(models.HWBalance.time.desc()).first()
    if available is not None:
        hwb.available += available
    if total is not None:
        hwb.total += total
    ses.add(hwb)
    try:
        ses.commit()
    except Exception as e:
        logger.exception(e)
        ses.rollback()
        ses.flush()


def process_txn(txid):
    client = create_client()
    txd = client.gettransaction(txid)
    confirmed = txd['confirmations'] >= CONFS
    for p, put in enumerate(txd['details']):
        if put['category'] == 'send':
            confirm_send(put['address'], put['amount'],
                         ref_id="%s:%s" % (txid, p))
        elif put['category'] == 'receive':
            process_receive("%s:%s" % (txid, p), put, confirmed)


lastblock = 0

def process_block():
    client = create_client()
    info = client.getinfo()
    global lastblock
    if info['blocks'] <= lastblock:
        return
    lastblock = info['blocks']
    creds = ses.query(models.Credit)\
        .filter(models.Credit.state == 'unconfirmed')\
        .filter(models.Credit.network == NETWORK)
    for cred in creds:
        txid = cred.ref_id.split(':')[0] or cred.ref_id
        txd = client.gettransaction(txid)
        if txd['confirmations'] >= CONFS:
            cred.state = 'complete'
            for p, put in enumerate(txd['details']):
                cred.ref_id = "%s:%s" % (txd['txid'], p)
            ses.add(cred)
    try:
        ses.commit()
    except Exception as e:
        logger.exception(e)
        ses.rollback()
        ses.flush()

    # update balances
    total = int(float(client.getbalance("*", 0)) * 1e8)
    avail = int(float(info['balance']) * 1e8)
    hwb = models.HWBalance(avail, total, CURRENCIES[0], NETWORK.lower())
    ses.add(hwb)
    try:
        ses.commit()
    except Exception as ie:
        ses.rollback()
        ses.flush()


def txn_processor(txn_queue):
    while True:
        txid = txn_queue.get()
        if txid is None:
            break
        process_txn(txid)

def block_processor(blk_queue):
    while True:
        blkid = blk_queue.get()
        if blkid is None:
            break
        process_block()

def pipe_reader(pipe, my_net, txn_queue, blk_queue):
    """
    Read notification messages from an open file and dispatch them to handler
    threads.

    Supports a pipe with multiple writers, as long as writes to the pipe are
    under PIPE_BUF (which is at least 512 bytes) to be atomic; see pipe(7).

    Returns at EOF (when there are no more writers on the pipe).
    """
    while True:
        line = pipe.readline()
        if len(line) == 0:
            break
        try:
            net, notify_type, data = line.split()
        except ValueError:
            logger.error('Bad notification %r', line)
            continue
        if net != my_net:
            logger.error('Bad notification network %s', net)
            continue
        if notify_type == 'transaction':
            txn_queue.put(data)
        elif notify_type == 'block':
            try:
                blk_queue.put_nowait(data)
            except Queue.Full:
                pass
        else:
            logger.error('Bad notification type %s', notify_type)


class Signal(Exception):
    pass

def sig_handler(sig, _):
    raise Signal(sig)


def main():
    """
    Entry point for the process. Creates a named pipe (if necessary) for
    reading notifications, sets its permissions, manages threads to process
    notifications, and repeatedly reads from the pipe.
    """
    my_net = NETWORK.lower()
    pipe_file = CFG.get(my_net, 'NOTIFY_PIPE')
    if not os.path.exists(pipe_file):
        os.mkfifo(pipe_file)
    os.chmod(pipe_file, mode=0620)
    if CFG.has_option(my_net, 'NOTIFY_GROUP'):
        group_name = CFG.get(my_net, 'NOTIFY_GROUP')
        gid = grp.getgrnam(group_name)[2]
        os.chown(pipe_file, os.geteuid(), gid)
    txn_queue = Queue.Queue()
    blk_queue = Queue.Queue(1)
    # ^ If multiple block notifications pile up, we only need to process one
    try:
        signal.signal(signal.SIGHUP, sig_handler)
        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)
        txn_thread = threading.Thread(target=txn_processor, args=(txn_queue,))
        blk_thread = threading.Thread(target=block_processor, args=(blk_queue,))
        txn_thread.start()
        blk_thread.start()
        while True:
            with open(pipe_file, 'rb') as pipe: # blocks until there's a writer
                pipe_reader(pipe, my_net, txn_queue, blk_queue)
    except Signal:
        # Gracefully shut down threads
        txn_queue.put(None)
        txn_thread.join()
        blk_queue.put(None)
        blk_thread.join()


if __name__ == "__main__":
    main()

