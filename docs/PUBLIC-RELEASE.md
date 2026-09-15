# Preparing a public source release

The current source contains no embedded personal server address, signing team, ingestion
credential, or fixed health-score cards. A new installation generates its own credentials;
Xcode signing values belong in ignored `Config/Local.xcconfig`. Missing data stays unavailable.
Protocol constants, documented algorithm thresholds, infrastructure defaults, and explicitly
synthetic unit-test vectors are necessary code, not simulated production measurements.

Review Git history separately from the current files. Deleting data from the
current tree does not erase earlier commits. Replacing published history requires
a coordinated migration, and cannot erase copies already downloaded elsewhere.
Use a GitHub no-reply commit email and keep health screenshots and recordings local.

## Clean source export

Start from a reviewed, committed checkout. A Git archive contains the current tracked tree,
without earlier commits, credentials, local telemetry, device builds, or ignored local settings:

```sh
python3 scripts/audit_public_source.py
gitleaks git --redact --no-banner .
git archive --format=zip --prefix=life/ -o ../life-source.zip HEAD
```

Extract into a new directory and run `gitleaks dir --redact --no-banner` there. Review the
export and test it before initializing a **new repository with a fresh first commit**. Preserve
the original private history separately. Never copy the working directory wholesale: ignored
archives, secrets, local configuration and test recordings can be present there.

Choose a license before calling the release open source. Life has no root license yet; a
public repository alone does not grant an open-source license. Keep the OpenStrap MIT notice
in `docs/licenses/OpenStrap.txt` for referenced protocol material. Dependency licenses and
existing research provenance also need to remain available.

NOOP is a research reference under PolyForm Noncommercial. No NOOP scoring code was copied
into the new strain/sleep implementation. Do not vendor or translate its code into a permissive
release without resolving the license. The proprietary WHOOP firmware used for offline
verification is not distributed with Life.

The audit is scoped: Gitleaks detects known credential patterns, and the source check rejects
tracked local data, private deployment paths, hard-coded card values and constant Grafana
series. Passing those checks is not a guarantee about every possible secret or license issue.
