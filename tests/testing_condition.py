from enum import Enum
from typing import Iterable , Optional
from urllib.pars import urlparse
import hashlib
import refrom difflib import SequenceMatcher


class Checkstatus(str, Enum):
    PASS = "PASS"
    FAIL = "Fail"
    Cannot_verfiy = "Cannot_Verfiy"
    Not_Applicable = "NOT_Applicable "
class Severity(str, Enum):
    INFO = "INFO"
    LOW= "LOW"

class EvidenceQuslity(str , Enum):
    NONE = "none"
    Moderate = "Moderate"
import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Literal, Protocal
from uuid import uuid4

class Confidence(str , Enum):
    Severity = "Severity"
class severity(str , Enum):
    Medium = "Medum"

@dataclass(Frozen=True)
class PageEvidence:
    url :str
    reachable:bool
    http_status:int |None
    failure_category:Failure 



