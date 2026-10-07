"""Retired historical endpoint-to-structure launcher.

Its fixed campaign and lossy interval selection cannot represent unresolved or
multi-component modern results. Legacy execution remains on py3Combine.
"""


def main():
    raise SystemExit(
        "Historical 2sigma structure generation is retired. Consume structured "
        "getIntervalFits results and explicitly qualify bounded component selection "
        "in a configured campaign before generating structures."
    )


if __name__ == "__main__":
    main()
