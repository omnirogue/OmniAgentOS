"""Cash / banking: bank balances, money deposited, and true cash expenses.

A cash-flow complement to the revenue / P&L store. Everything here is REAL money
and every external call is a READ (GET through the credential broker); no module
in this package can move, transfer, or pay money.
"""
