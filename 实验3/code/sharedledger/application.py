"""Wire the desktop entry point to the business services."""

from .accounts import Accounts, Ledgers
from .entries import Entries
from .queries import Queries
from .review import Review
from .settlement import Settlements
from .storage import Database


class Application:
    def __init__(self, database_path):
        database = Database(database_path)
        self.accounts = Accounts(database)
        self.ledgers = Ledgers(database, self.accounts)
        self.entries = Entries(database, self.accounts)
        self.review = Review(database, self.accounts)
        self.queries = Queries(database, self.accounts)
        self.settlements = Settlements(database, self.accounts)
