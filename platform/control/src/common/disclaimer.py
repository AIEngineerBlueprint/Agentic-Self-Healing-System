"""Single source of truth for the demo disclaimer.

Surfaced in three places, deliberately:
  1. Every calculation response body (`disclaimer` field).
  2. The product UI, persistently, next to the result.
  3. Each service's OpenAPI description.

This is not boilerplate. The whole premise of the Scenario 3 policy gate is that
emission factors are the kind of number that ends up in a public disclosure --
which is why an agent may flag them but may never autonomously change them.
Saying plainly that this instance is NOT fit for that purpose is what makes the
distinction honest rather than theatrical.
"""

SHORT = "Demo data. Not for reporting or disclosure use."

LONG = (
    "This is a demonstration system. Emission factors are real published values "
    "from cited sources (DEFRA 2024, CEA v20, EPA eGRID2022, Scarborough et al. "
    "2014), but the dataset is incomplete, the boundary is simplified, and the "
    "results are indicative only. Do not use these figures for carbon "
    "accounting, regulatory reporting, offset purchasing, or any public "
    "disclosure."
)

OPENAPI_NOTE = (
    "\n\n---\n\n**Disclaimer.** " + LONG + "\n\nThis service is part of the "
    "Agentic Self-Healing System (ASHS) demo and exposes a `/_demo/bug` "
    "endpoint for controlled fault injection. It is not a production service."
)
