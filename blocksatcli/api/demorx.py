#!/usr/bin/env python3
"""
Read API data directly via internet and output to pipe
"""

import json
import logging
import time

import requests
import sseclient

from . import net
from .order import ApiOrder
from .pkt import BlocksatPkt, BlocksatPktHandler

logger = logging.getLogger(__name__)
MAX_SEQ_NUM = 2**31  # Maximum transmission sequence number


class DemoRx():
    """Demo receiver
    """
    def __init__(self,
                 server,
                 socks,
                 kbps,
                 tx_event,
                 channel,
                 regions=None,
                 tls_cert=None,
                 tls_key=None):
        """ DemoRx Constructor

        Args:
            server   : API server address where the order lives
            socks    : Instances of UdpSock over which to send the packets
            kbps     : Target bit rate in kbps
            tx_event : SSE event to use as trigger for transmissions
            channel  : API channel number
            regions  : Regions covered by the transmission (for Tx
                       confirmation)
            tls_key  : API client key (for Tx confirmation)
            tls_cer  : API client certificate (for Tx confirmation)


        """
        # Validate args
        assert (isinstance(socks, list))
        assert (all([isinstance(x, net.UdpSock) for x in socks]))

        # Configs
        self.server = server
        self.socks = socks
        self.kbps = kbps
        self.tx_event = tx_event
        self.channel = channel
        self.regions = regions
        self.tls_cert = tls_cert
        self.tls_key = tls_key

        # State
        self.last_seq_num = None

    def _send_pkts(self, pkts):
        """Transmit Blocksat packets of the API message over all sockets

        Transmit and sleep (i.e., block) to guarantee the target bit rate.

        Args:
            pkts : List of BlocksatPkt objects to be send over sockets

        """
        assert (isinstance(pkts, list))
        assert (all([isinstance(x, BlocksatPkt) for x in pkts]))

        byte_rate = self.kbps * 1e3 / 8  # bytes / sec
        next_tx = time.time()
        for i, pkt in enumerate(pkts):
            # Send the same packet on all sockets
            for sock in self.socks:
                sock.send(pkt.pack())
                logger.debug("Send packet %d - %d bytes" % (i, len(pkt)))

            # Throttle
            tx_delay = len(pkt) / byte_rate
            next_tx += tx_delay
            sleep = next_tx - time.time()
            if (sleep > 0):
                time.sleep(sleep)

    def _handle_event(self, event):
        """Handle event broadcast by the SSE server

        Args:
            event : Event generated by the sseclient

        """
        # Parse the order corresponding to the event
        order = json.loads(event.data)

        # Debug
        logger.debug("Order: " + json.dumps(order, indent=4, sort_keys=True))

        # Proceed when the event matches the target Tx trigger event
        if (order["status"] != self.tx_event):
            return

        # Sequence number
        seq_num = order["tx_seq_num"]

        # If the sequence number has rolled back, maybe it is because the
        # server has restarted the sequence numbers (not uncommon on test
        # environments). In this case, restart the sequence.
        if (self.last_seq_num is not None and seq_num < self.last_seq_num):
            logger.warning("Tx sequence number rolled back from {} to "
                           "{}".format(self.last_seq_num, seq_num))
            self.last_seq_num = None

        rx_pending = True
        while (rx_pending):
            # Receive all messages until caught up
            if (self.last_seq_num is None):
                next_seq_num = seq_num
            else:
                next_seq_num = self.last_seq_num + 1

            # Keep track of the last processed sequence number
            self.last_seq_num = next_seq_num

            # Is this an interation to catch up with a sequence
            # number gap or a normal transmission iteration?
            if (seq_num == next_seq_num):
                rx_pending = False
            else:
                logger.info("Catch up with transmission %d" % (next_seq_num))

            logger.info("Message %-5d\tSize: %d bytes\t" %
                        (next_seq_num, order["message_size"]))

            # Get the API message data
            order = ApiOrder(self.server,
                             seq_num=next_seq_num,
                             tls_cert=self.tls_cert,
                             tls_key=self.tls_key)
            data = order.get_data()

            if (data is None):
                # Empty message. There is nothing else to do.
                continue

            # Split API message data into Blocksat packet(s)
            tx_handler = BlocksatPktHandler()
            tx_handler.split(data, next_seq_num, self.channel)
            pkts = tx_handler.get_frags(next_seq_num)

            logger.debug("Transmission is going to take: "
                         "{:6.2f} sec".format(len(data) * 8 / (self.kbps)))

            # Send the packet(s)
            self._send_pkts(pkts)

            # Send transmission confirmation to the server
            order.confirm_tx(self.regions)

    def run(self):
        """Run the demo-rx transmission loop"""
        logger.info("Connecting with Satellite API server...")
        while (True):
            try:
                # Server-sent Events (SSE) Client
                r = requests.get(self.server + "/subscribe/transmissions",
                                 stream=True,
                                 cert=(self.tls_cert, self.tls_key))
                r.raise_for_status()
                client = sseclient.SSEClient(r)
                logger.info("Connected. Waiting for events...\n")

                # Continuously wait for events
                for event in client.events():
                    self._handle_event(event)

            except requests.exceptions.ChunkedEncodingError as e:
                logger.debug(e)
                pass

            except requests.exceptions.ConnectionError as e:
                logger.debug(e)
                time.sleep(2)
                pass

            except requests.exceptions.RequestException as e:
                logger.debug(e)
                time.sleep(2)
                pass

            except KeyboardInterrupt:
                exit()

            logger.info("Reconnecting...")
