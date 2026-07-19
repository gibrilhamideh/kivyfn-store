from enum import Enum


class ForeignKeyPolicy(str, Enum):
    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
