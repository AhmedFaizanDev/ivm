"""Change-capture adapters (Milestone 2): normalize a backend's writes into the
engine's Z-set delta format. An adapter mutates real data and forwards the
resulting deltas; the recompute oracle then runs over that real data, so a
capture bug is indistinguishable from an engine bug and the same oracle catches
both."""
