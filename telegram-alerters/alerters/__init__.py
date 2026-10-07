"""Two independent Telegram alerters for on-chain ERC-20 transfers.

* ``alerters.team_wallets_bot``   - transfers to / from team wallets.
* ``alerters.exchange_flows_bot`` - large (>= $80k by default) CEX deposits / withdrawals.

Both bots share only the helper library in ``alerters.common``; they have
separate configs, Telegram tokens, state files and processes.
"""
