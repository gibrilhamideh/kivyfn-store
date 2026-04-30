"""
Register FnKivy-ui widgets to use without import.
"""

from kivy.factory import Factory


def register():
    register = Factory.register

    # ---) Context
    register("FnBinder", module="kivyfn.uibase.binder")
