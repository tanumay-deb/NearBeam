from setuptools import setup, find_packages
from pathlib import Path

this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8") if (this_directory / "README.md").exists() else ""

setup(
    name="landrop",
    version="1.3.1",
    description="High-Speed Local Wi-Fi File Sharing & Explorer with Multi-Device Mesh",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Tanumay Goswami",
    author_email="tanumaygoswami2001@gmail.com",
    url="https://github.com/tanumay-deb/LANDrop",
    py_modules=["main", "gui"],
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "": [
            "templates/*",
            "static/**/*",
            "assets/*",
            "allow_firewall.bat",
        ],
    },
    install_requires=[
        "flask>=3.0.0",
        "flask-cors>=4.0.0",
        "qrcode>=8.0.0",
        "Pillow>=10.0.0",
        "PySide6>=6.5.0",
        "zeroconf>=0.130.0",
    ],
    entry_points={
        "console_scripts": [
            "landrop = main:main",
        ],
        "gui_scripts": [
            "landrop-gui = main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS",
        "Operating System :: POSIX :: Linux",
        "Topic :: Communications :: File Sharing",
    ],
    python_requires=">=3.8",
)
