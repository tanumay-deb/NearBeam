from setuptools import setup, find_namespace_packages, find_packages

# Name, version, dependencies, commands and classifiers live in pyproject.toml, which overrides them here.
setup(
    py_modules=["main", "gui"],
    # The app reads templates/, static/ and assets/ beside core/, so they install there as data
    # folders, with their files listed in MANIFEST.in.
    # shortcut: generic top-level names, like core/ and ui/; move all into a nearbeam/ package if one clashes.
    packages=find_packages() + find_namespace_packages(include=["templates", "static*", "assets*"]),
    include_package_data=True,
)
