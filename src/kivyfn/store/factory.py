"""
Register FnKivy-ui widgets to use without import.
"""

from kivy.factory import Factory
from kivy.base import Builder


def register():
    register = Factory.register

    # ---) Context
    register("FnBinder", module="kivyfn.uibase.binder")

    Builder.load_string(
        """
        #: import store kivyfn.store.store
        #: import Address kivyfn.store.Address
        """
    )
