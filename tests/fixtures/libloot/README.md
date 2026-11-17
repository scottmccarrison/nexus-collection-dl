# libloot test plugins

Real Bethesda plugin files used to exercise LOOT sorting end to end. Taken
from [Ortham/testing-plugins](https://github.com/Ortham/testing-plugins) at
tag `1.6.2`, the same fixture set libloot's own CI downloads. MIT licensed,
(c) 2014 Oliver Hamlet; see `LICENSE` in this directory.

These are xEdit-built plugins with documented master/dependent edges, which is
what makes a sort verifiable: feeding them in a wrong order and observing the
dependencies come out respected proves the sorter did real work.

## Files

| File | Size | Role |
|---|---|---|
| `Blank.esm` | 67 KB | master |
| `Blank - Master Dependent.esp` | 734 B | overrides 2 records from `Blank.esm` |
| `Blank.esp` | 1 KB | standalone, no masters |
| `Blank - Plugin Dependent.esp` | 422 B | overrides 1 record from `Blank.esp` |

So a correct load order has `Blank.esm` before `Blank - Master Dependent.esp`,
and `Blank.esp` before `Blank - Plugin Dependent.esp`. `Blank.esm` is the large
one because its `ONAM` subrecord carries 16,384 FormID references.

## Why these exist

`test_loot_sort.py` pins the *call shape* against a stub bindings module: it
catches calling a function libloot does not have, or deriving a `GameType`
member name wrongly. It does not prove libloot will actually sort anything.

`test_loot_sort_e2e.py` closes that gap. It runs the real bindings over these
plugins and asserts the dependency order comes out right. It is skipped unless
libloot is installed, which it is not in CI, so it is a local proof rather
than a CI gate. Run it with the bindings present:

    pip install <a libloot wheel from one of this repo's releases>
    pytest tests/test_loot_sort_e2e.py -v

Do not replace these with hand-built minimal TES4 files. libloot rejects
malformed plugins (`is_valid_plugin` returns False) and its loader can hang on
them rather than fail cleanly.
