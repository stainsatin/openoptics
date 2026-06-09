# Copyright (c) Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V.
#
# Flare-specific ns-3 integration tests. These are intentionally
# mechanism-level smoke tests: they verify that Flare credit/data packets
# traverse the existing OpenOptics ns-3 pipeline and that transport
# counters are queryable. Paper-scale trend reproduction lives in
# scripts/examples, not unit tests.

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.ns3_helpers import ns3_available, skip_if_no_ns3


@skip_if_no_ns3
class Ns3FlareIntegrationTests(unittest.TestCase):
    def setUp(self):
        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"

    def tearDown(self):
        os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
        os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)
        if ns3_available():
            from ns import ns

            ns.Simulator.Destroy()

    def _direct_net(self, *, profile="55us", stop_s=0.02):
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend

        backend = Ns3Backend()
        with patch("openoptics.Toolbox.create_backend", return_value=backend):
            net = Toolbox.BaseNetwork(
                name=f"flare_{profile}_direct",
                backend="ns3",
                nb_node=4,
                time_slice_duration_us=55 if profile == "55us" else 15,
                guardband_ms=0,
                ocs_tor_link_bw_gbps=100,
                tor_host_link_bw_gbps=100,
                use_webserver=False,
                simulation_stop_s=stop_s,
                flare_profile=profile,
            )
            net.deploy_topo(OpticalTopo.round_robin(nb_node=4))
            net.deploy_routing(
                OpticalRouting.routing_direct(net.get_topo()),
                routing_mode="Per-hop",
            )
        return net, backend

    def test_single_flare_flow_completes_and_reports_counters(self):
        net, backend = self._direct_net(profile="55us", stop_s=0.02)

        installed = (
            net.flare_traffic()
            .flow(0, 1, 16_384, start_s=0.0001, duration_s=0.01)
            .install()
        )
        net.start()

        self.assertEqual(len(installed), 1)
        stats = installed[0].stats()
        self.assertIsNotNone(stats)
        self.assertGreater(stats.credits_sent, 0, stats)
        self.assertGreater(stats.credits_received, 0, stats)
        self.assertGreater(stats.credits_admitted, 0, stats)
        self.assertGreater(stats.data_packets_sent, 0, stats)
        self.assertGreater(stats.data_packets_received, 0, stats)
        self.assertGreater(stats.fct_s, 0.0, stats)
        self.assertGreater(stats.throughput_bps, 0.0, stats)

    def test_flare_incast_makes_progress_without_deadlock(self):
        net, backend = self._direct_net(profile="15us", stop_s=0.03)

        installed = (
            net.flare_traffic()
            .many_to_one([0, 1, 2], dst=3, size_bytes=8_192,
                         start_s=0.0001, duration_s=0.02)
            .install()
        )
        net.start()

        self.assertEqual(len(installed), 3)
        completed = 0
        for flow in installed:
            stats = flow.stats()
            self.assertIsNotNone(stats)
            self.assertGreater(stats.credits_sent, 0, stats)
            self.assertGreater(stats.credits_received, 0, stats)
            self.assertGreater(stats.data_packets_sent, 0, stats)
            if stats.data_packets_received > 0:
                completed += int(stats.fct_s == stats.fct_s and stats.fct_s > 0)
        self.assertGreaterEqual(completed, 1)

    def test_profile_parameters_reach_tor_apps(self):
        net, backend = self._direct_net(profile="15us", stop_s=0.005)

        installed = (
            net.flare_traffic()
            .flow(0, 1, 2_048, start_s=0.0001, duration_s=0.002)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        self.assertIsNotNone(stats)
        # The 15us profile has a very small credit threshold; the exact
        # admission/drop balance depends on the active slice, but at least
        # one ToR-side Flare counter should move.
        moved = (
            stats.credits_admitted +
            stats.credits_dropped +
            stats.credits_wasted
        )
        self.assertGreater(moved, 0)


if __name__ == "__main__":
    unittest.main()
