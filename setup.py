from setuptools import find_packages, setup

setup(
    name="inventree-build-blockers",
    version="0.1.1",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    entry_points={
        "inventree_plugins": [
            "BuildBlockers = build_blockers.build_blockers:BuildBlockers"
        ]
    },
    author="Per Vices Corporation",
    description=(
        "Export Build Order component blockers with "
        "purchase order target dates"
    ),
    platforms="any",
)
