# Contributing to Locomotive

Thanks for taking the time to contribute! Locomotive is an open-source project
and contributions of all kinds are welcome — bug reports, documentation fixes,
new features, and feedback on the design.

## Scope

Locomotive is a **declarative load-testing runner and regression analyzer built
on top of [Locust](https://locust.io)**. Its goal is to make load testing cheap
to adopt for small teams: describe a scenario in JSON/YAML, generate a
locustfile, run it in CI, and gate on performance regressions.

Things that fit the project well:

- Improvements to the config format, scenario generation, or the runtime resolver
- OpenAPI generation, validation (`loco validate`), and drift detection (`loco diff`)
- Reporting, baseline/regression analysis, and CI ergonomics
- Documentation, examples, and better error messages

Things that are generally **out of scope**: replacing Locust's load-generation
engine, hosted/SaaS features, and integrations that belong in a separate plugin.
If you're unsure whether an idea fits, open an issue to discuss it before writing
code — it saves everyone time.

## Development setup

Locomotive targets **Python 3.9+**.

```bash
# Fork the repo on GitHub, then:
git clone https://github.com/<your-username>/locomotive.git
cd locomotive

# Install in editable mode with dev dependencies
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running the tests

The whole suite runs with:

```bash
pytest
```

Every change should keep the suite green. The generated-code tests compile and
exec the produced locustfile against a stubbed Locust, so they catch most
regressions in the generator itself.

## Making a change

1. Create a branch off `master`.
2. Make your change and **add or update tests** — new behavior without a test is
   hard to accept.
3. If you touch the config surface, make sure `loco validate` still accepts the
   example configs and add validation for any new fields.
4. Run `pytest` and confirm everything passes.
5. Keep the diff focused: one logical change per pull request is much easier to
   review than a large mixed one.

## Submitting a pull request

- Fill in the pull request template so reviewers have context.
- Reference the issue your PR addresses (e.g. "Closes #123") when there is one.
- Describe *what* changed and *why*, and note anything reviewers should look at
  closely.

## Reporting bugs and requesting features

Please use the issue templates. For bugs, the most helpful reports include the
Locomotive version, the relevant slice of your config, the command you ran, and
what you expected versus what happened.

## Licensing of contributions

Locomotive is released under the [MIT License](LICENSE). By submitting a
contribution, you agree that your contribution is licensed under the same MIT
License (inbound = outbound). No separate contributor license agreement is
required.
