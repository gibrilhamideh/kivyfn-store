from enum import Enum

class ConstraintType(str, Enum):
    UNIQUE = "unique"
    FK_MISSING = "fk_missing"
    FK_RESTRICT = "fk_restrict"
    FOREIGN_KEY = "foreign_key"


