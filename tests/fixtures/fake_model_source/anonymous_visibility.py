"""FX-MS-04's ``anonymous_not_found_observable`` dimension, per source.

TST V1.2 moved "a missing repository is only observable with a credential" from
a *source-independent property* to an explicit per-source dimension, because
``FX-MS-09`` disproved it as a general rule: it is Hugging Face's behaviour
(anonymous reads answer 401 and fold "missing" together with "invisible"),
while ModelScope answers 404 with ``Code: 10010205001`` to an anonymous read.

The dimension must be asserted **in both directions**.  Asserting only the
``False`` side -- "these two conclusions are indistinguishable" -- is satisfied
by an implementation that collapses everything, so the ``True`` side has to
require that the two conclusions can be told apart.

This module holds nothing but the declaration, and it imports nothing from the
rest of the fixture package, so a positive control can load a copy of it with
the two values swapped and re-run the same judgement against the same recorded
responses (see ``TC-DL-20``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from holm_domain import ModelSourceType

ANONYMOUS_NOT_FOUND_OBSERVABLE: Final[Mapping[ModelSourceType, bool]] = MappingProxyType(
    {
        # FX-MS-08: an anonymous read of a repository that does not exist
        # answers 401, whose body text talks about a user name and password
        # although the request carried no credential at all.  Only a credential
        # -bearing read reaches 404.
        ModelSourceType.HUGGING_FACE: False,
        # FX-MS-09: an anonymous read of a repository that does not exist
        # answers 404 with Code 10010205001, and the recording made with an
        # invalid credential is byte-identical to it apart from the RequestId.
        ModelSourceType.MODELSCOPE: True,
    }
)


def anonymous_not_found_observable(source: ModelSourceType) -> bool:
    """Whether this source lets the platform observe "does not exist" anonymously.

    A source with no entry raises ``KeyError`` rather than defaulting: a new
    source must state its own observed behaviour, and neither default would be
    harmless -- ``False`` would send an operator to configure a credential for a
    repository they merely mistyped, ``True`` would claim a distinction the
    source does not make.
    """

    return ANONYMOUS_NOT_FOUND_OBSERVABLE[source]


__all__ = ["ANONYMOUS_NOT_FOUND_OBSERVABLE", "anonymous_not_found_observable"]
