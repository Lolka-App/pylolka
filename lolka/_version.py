"""
The MIT License (MIT)

Copyright (c) 2015-2021 Rapptz
Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import datetime

from typing_extensions import TypedDict, deprecated

__all__ = ("__version__", "VersionInfo", "version_info")

from typing import Literal, NamedTuple

# Lolka: версия дистрибутива lolka.pycord (читается setuptools через attr),
# version_info при этом отражает версию py-cord, на которой основан форк.
__version__ = "0.1.0"


class AdvancedVersionInfo(TypedDict):
    serial: int
    build: int | None
    commit: str | None
    date: datetime.date | None


class VersionInfo(NamedTuple):
    major: int
    minor: int
    micro: int
    releaselevel: Literal["alpha", "beta", "candidate", "final"]

    # We can't set instance attributes on a NamedTuple, so we have to use a
    # global variable to store the advanced version info.
    @property
    def advanced(self) -> AdvancedVersionInfo:
        return _advanced

    @advanced.setter
    def advanced(self, value: object) -> None:
        global _advanced
        _advanced = value

    @property
    @deprecated(
        "VersionInfo.release_level is deprecated since version 2.4, consider using releaselevel instead."
    )
    def release_level(self) -> Literal["alpha", "beta", "candidate", "final"]:
        return self.releaselevel

    @property
    @deprecated(
        'VersionInfo.serial is deprecated since version 2.4, consider using .advanced["serial"] instead.'
    )
    def serial(self) -> int:
        return self.advanced["serial"]

    @property
    @deprecated(
        'VersionInfo.build is deprecated since version 2.4, consider using .advanced["build"] instead.'
    )
    def build(self) -> int | None:
        return self.advanced["build"]

    @property
    @deprecated(
        'VersionInfo.commit is deprecated since version 2.4, consider using .advanced["commit"] instead.'
    )
    def commit(self) -> str | None:
        return self.advanced["commit"]

    @property
    @deprecated(
        'VersionInfo.date is deprecated since version 2.4, consider using .advanced["date"] instead.'
    )
    def date(self) -> datetime.date | None:
        return self.advanced["date"]


version_info: VersionInfo = VersionInfo(
    major=2, minor=8, micro=1, releaselevel="final"
)

_advanced = AdvancedVersionInfo(serial=0, build=None, commit=None, date=None)
