# Third-party notices and provenance record

This file is an inventory, not a legal opinion and not a substitute for the
upstream licence texts. A URL in this file identifies the project's declared
source reference; it does not by itself prove how a bundled binary was built.

## Bundled binary: KRKRZ `Version.dll`

| Field | Recorded value |
| --- | --- |
| Repository path | `assets/krkrz/Version.dll` |
| Size | 52,224 bytes |
| SHA-256 | `B2CADE6DD86319E9B71DAE5CC4CBBBDEC3965139D5464BEDA26E44245F6BBDA5` |
| Declared upstream/source reference | <https://github.com/1F1E33-float32/Fuck_Cxdec_Check> |
| Upstream repository licence indicator | GPL-3.0 `LICENSE.txt`; a commit-pinned copy is retained at [`LICENSES/Fuck_Cxdec_Check-GPL-3.0.txt`](LICENSES/Fuck_Cxdec_Check-GPL-3.0.txt), but applicability to this exact binary is **not verified** |
| Upstream reference inspected for this release | [`cb59274f8300face3f0158d5d68f27b83c2ea449`](https://github.com/1F1E33-float32/Fuck_Cxdec_Check/commit/cb59274f8300face3f0158d5d68f27b83c2ea449) (current `master` observed 2026-07-11; **not asserted as the binary's source commit**) |
| Exact binary tag or commit | **Not recorded / not verified** |
| Upstream binary releases | None observed; the candidate repository contains source, not this prebuilt DLL |
| Reproducible build instructions | **Not recorded / not verified** |
| Authenticode signature | `NotSigned` when inspected on Windows |
| Release decision | Retained in IceHub source and binary releases with this unresolved provenance disclosure |

The hash above was calculated from the file currently present in this
repository. It establishes the identity of that file only. It does **not**
establish that the binary was produced by the linked repository or inspected
commit, that the binary is unmodified, or that all redistribution obligations
have been satisfied. The embedded PDB path and absent version metadata are not
proof of origin.

The project owner has chosen to publish the existing DLL while retaining this
limitation. Maintainers should still complete the following provenance work:

1. identify and record the exact upstream commit or release;
2. obtain and retain the applicable licence text;
3. confirm that the licence permits binary redistribution in this product;
4. build from reviewed source where practical and compare the output;
5. update this file if the DLL changes, including its size and SHA-256.

Every automated IceHub release verifies that the packaged payload is a PE file
and publishes a checksum for the final `IceHub.exe`. Those checks prevent an
accidental packaging omission or undetected artifact change; they do not resolve
the DLL's historical provenance.

The candidate upstream source vendors Microsoft Detours-derived code. The
Microsoft Detours 4.0.1 MIT licence is retained at
[`LICENSES/Microsoft-Detours-MIT.md`](LICENSES/Microsoft-Detours-MIT.md). This
notice records the candidate source dependency; it does not prove that the
bundled DLL was produced from that exact tree.

## Downloaded at runtime (not bundled in the source tree)

IceHub can download releases from these configured upstream repositories:

- OpenSteamTool: <https://github.com/OpenSteam001/OpenSteamTool>
- Steamless: <https://github.com/atom0s/Steamless>

Their exact release versions and files vary at runtime. Their licences,
signatures/checksums, notices, and redistribution terms have **not** been
verified by this inventory. Do not mirror or redistribute downloaded assets
without reviewing the exact selected release.

## Python dependencies

Direct Python dependencies are listed in `requirements.txt`, build/test
dependencies in `requirements-dev.txt`, and the reviewed release environment
in `requirements-release.txt`. Package metadata installed by `pip` remains the
authoritative input for a release-specific dependency inventory. A future
release process should add an SBOM or `pip-licenses` report and retain all
required licence texts.

IceHub is distributed under GPL-3.0-only to align with the GPL release path of
PyQt6 and PyQt6-Fluent-Widgets. Commercial distribution under different terms
would require a separate dependency and legal review.

## Product artwork

`assets/icon.ico` and `assets/icon.png` are distributed as IceHub product
artwork. Their original creation record is not preserved in this repository;
replace or document them before claiming third-party provenance or granting
separate reuse rights beyond the IceHub distribution.
