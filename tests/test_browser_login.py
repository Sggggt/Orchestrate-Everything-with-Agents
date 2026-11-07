"""Browser tickets require possession, expire, and never reveal the control credential."""

import unittest
from unittest.mock import patch

from oea.contracts.errors import RuntimeFault
from oea.security.browser import BrowserLogins


class BrowserLoginTests(unittest.TestCase):
    def test_ticket_is_single_use_and_unknown_values_do_not_consume_valid_tickets(self):
        logins = BrowserLogins()
        issued = logins.issue()
        with self.assertRaises(RuntimeFault):
            logins.redeem("unknown")
        logins.redeem(issued["ticket"])
        with self.assertRaises(RuntimeFault):
            logins.redeem(issued["ticket"])
        self.assertFalse(logins.pending)

    def test_ticket_expiry_uses_elapsed_time(self):
        logins = BrowserLogins()
        with patch("oea.security.browser.time.monotonic", return_value=100):
            issued = logins.issue()
        with patch("oea.security.browser.time.monotonic", return_value=161):
            with self.assertRaises(RuntimeFault):
                logins.redeem(issued["ticket"])
        self.assertFalse(logins.pending)
