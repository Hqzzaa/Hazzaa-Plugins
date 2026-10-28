import os
import sys

_bundle = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
os.environ["TCL_LIBRARY"] = os.path.join(_bundle, "_tcl_data")
os.environ["TK_LIBRARY"] = os.path.join(_bundle, "_tk_data")
os.environ["TCLLIBPATH"] = os.path.join(_bundle, "tcl8")
