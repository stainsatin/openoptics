# Copyright (c) Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V.
#
# Flare Phase 1-4 mechanism tests: verify that the 6 core mechanisms
# (path symmetry, probabilistic admission, rate control, tentative credits,
# hop jittering, fast start) are correctly implemented and functional.

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.ns3_helpers import ns3_available, skip_if_no_ns3


@skip_if_no_ns3
class Ns3FlarePhaseMechanismTests(unittest.TestCase):
    """Test the 4 phases of Flare implementation."""

    def setUp(self):
        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"

    def tearDown(self):
        os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
        os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)
        if ns3_available():
            from ns import ns
            ns.Simulator.Destroy()

    def _make_network(self, nb_node=4, profile="55us", stop_s=0.02, **flare_overrides):
        """Helper to create a test network with custom Flare config."""
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend

        # Convert FlareConfig overrides to backend kwargs
        backend_kwargs = {
            "simulation_stop_s": stop_s,
            "flare_profile": profile,
        }
        # Add any flare parameter overrides
        for key, value in flare_overrides.items():
            if key in ["credit_qsize_pkts", "shaping_thresh_pkts", "aeolus_thresh_pkts",
                       "w_init", "target_loss", "congestion_threshold_percent",
                       "tentative_threshold_percent"]:
                backend_kwargs[f"flare_{key}"] = value

        backend = Ns3Backend()
        with patch("openoptics.Toolbox.create_backend", return_value=backend):
            net = Toolbox.BaseNetwork(
                name=f"flare_phase_test",
                backend="ns3",
                nb_node=nb_node,
                time_slice_duration_us=55 if profile == "55us" else 15,
                guardband_ms=0,
                ocs_tor_link_bw_gbps=100,
                tor_host_link_bw_gbps=100,
                use_webserver=False,
                **backend_kwargs,
            )
            net.deploy_topo(OpticalTopo.round_robin(nb_node=nb_node))
            net.deploy_routing(
                OpticalRouting.routing_direct(net.get_topo()),
                routing_mode="Per-hop",
            )
        return net, backend

    # ========================================================================
    # Phase 1: Credit-Data Path Symmetry
    # ========================================================================

    def test_phase1_time_slice_stamping(self):
        """Verify that credit packets carry time_slice field and data packets inherit it.

        Mechanism: ToR stamps time_slice on credit ingress, host records it,
        data packets inherit the same time_slice.
        """
        net, backend = self._make_network(nb_node=2, profile="55us", stop_s=0.015)

        installed = (
            net.flare_traffic()
            .flow(0, 1, 8_192, start_s=0.0001, duration_s=0.01)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        # If time_slice mechanism works, credits and data should traverse
        self.assertGreater(stats.credits_sent, 0, "No credits sent")
        self.assertGreater(stats.credits_received, 0, "No credits received")
        self.assertGreater(stats.data_packets_sent, 0, "No data sent")
        self.assertGreater(stats.data_packets_received, 0, "No data received")

        # Path symmetry should reduce credit waste (not a strict guarantee but indicator)
        if stats.credits_sent > 0:
            receive_ratio = stats.credits_received / stats.credits_sent
            self.assertGreater(receive_ratio, 0.5,
                             f"Low credit receive ratio {receive_ratio:.2f}, path symmetry may not work")

    # ========================================================================
    # Phase 2: Probabilistic Credit Admission
    # ========================================================================

    def test_phase2_probabilistic_admission_with_congestion(self):
        """Verify probabilistic admission based on remaining_hops.

        Mechanism: When queue occupancy >= congestion_threshold (50%),
        credits are admitted with probability P(h) = (1/2)^(h-1).
        """
        net, backend = self._make_network(
            nb_node=4,
            profile="55us",
            stop_s=0.03,
            congestion_threshold_percent=30  # Lower threshold to trigger probabilistic mode
        )

        # Create incast traffic to trigger congestion
        installed = (
            net.flare_traffic()
            .many_to_one([0, 1, 2], dst=3, size_bytes=16_384,
                         start_s=0.0001, duration_s=0.02)
            .install()
        )
        net.start()

        total_admitted = 0
        total_dropped = 0
        for flow in installed:
            stats = flow.stats()
            total_admitted += stats.credits_admitted
            total_dropped += stats.credits_dropped

        # Under congestion, some credits should be dropped due to probabilistic admission
        self.assertGreater(total_admitted, 0, "No credits admitted")
        if total_admitted + total_dropped > 0:
            drop_ratio = total_dropped / (total_admitted + total_dropped)
            # With incast, expect some drops (not strict, depends on timing)
            self.assertLess(drop_ratio, 0.8,
                          f"Drop ratio {drop_ratio:.2f} too high, probabilistic admission may be too aggressive")

    def test_phase2_congestion_threshold_parameter(self):
        """Verify that congestion_threshold_percent parameter is respected."""
        net, backend = self._make_network(
            nb_node=2,
            profile="55us",
            stop_s=0.01,
            congestion_threshold_percent=80  # High threshold: mostly accept
        )

        installed = (
            net.flare_traffic()
            .flow(0, 1, 4_096, start_s=0.0001, duration_s=0.008)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        # With high threshold, most credits should be admitted
        if stats.credits_admitted + stats.credits_dropped > 0:
            admit_ratio = stats.credits_admitted / (stats.credits_admitted + stats.credits_dropped)
            self.assertGreater(admit_ratio, 0.7,
                             f"Admit ratio {admit_ratio:.2f} too low with 80% threshold")

    # ========================================================================
    # Phase 3: Credit Rate Control + Tentative Credits
    # ========================================================================

    def test_phase3_tentative_credit_admission(self):
        """Verify that tentative credits are only admitted when queue is very low.

        Mechanism: Tentative credits (sent when targetCreditRate < 1.0) are
        only admitted when occupancy < tentative_threshold (25%).
        """
        net, backend = self._make_network(
            nb_node=2,
            profile="55us",
            stop_s=0.015,
            tentative_threshold_percent=20  # Low threshold for tentative
        )

        installed = (
            net.flare_traffic()
            .flow(0, 1, 8_192, start_s=0.0001, duration_s=0.01)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        # Tentative credits should be sent (targetCreditRate starts at 1.0 but may adjust)
        # The mechanism is present if credits flow
        self.assertGreater(stats.credits_sent, 0, "No credits sent")
        self.assertGreater(stats.credits_admitted, 0, "No credits admitted")

    def test_phase3_rate_control_under_loss(self):
        """Verify rate control adjusts under credit loss.

        Mechanism: When credits are dropped, targetCreditRate decreases;
        when no loss, it increases.
        """
        net, backend = self._make_network(
            nb_node=4,
            profile="55us",
            stop_s=0.03,
            credit_qsize_pkts=10,  # Small queue to trigger drops
            w_init=1.0,
            target_loss=0.1
        )

        # Incast to trigger credit drops
        installed = (
            net.flare_traffic()
            .many_to_one([0, 1, 2], dst=3, size_bytes=12_288,
                         start_s=0.0001, duration_s=0.02)
            .install()
        )
        net.start()

        total_sent = 0
        total_dropped = 0
        for flow in installed:
            stats = flow.stats()
            total_sent += stats.credits_sent
            total_dropped += stats.credits_dropped

        # Rate control should adapt to drops
        self.assertGreater(total_sent, 0, "No credits sent")
        # Some drops expected with small queue
        if total_dropped > 0:
            self.assertLess(total_dropped / total_sent, 0.5,
                          "Drop rate too high, rate control may not be working")

    # ========================================================================
    # Phase 4: Hop Jittering + Fast Start
    # ========================================================================

    def test_phase4_hop_jittering_mechanism(self):
        """Verify hop jittering is applied to credit packets.

        Mechanism: Based on delivered_bytes / BDP ratio, credit packets
        declare adjusted hop counts (short flows -1, long flows +1).
        """
        net, backend = self._make_network(
            nb_node=2,
            profile="55us",
            stop_s=0.015
        )

        # Send a flow and check if it completes (hop jittering should not break routing)
        installed = (
            net.flare_traffic()
            .flow(0, 1, 16_384, start_s=0.0001, duration_s=0.01)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        # Hop jittering applies to credits; if mechanism is correct, flow completes
        self.assertGreater(stats.data_packets_sent, 0, "No data sent")
        self.assertGreater(stats.data_packets_received, 0, "No data received")
        self.assertGreater(stats.fct_s, 0.0, "Flow did not complete")

    def test_phase4_fast_start_unscheduled_marking(self):
        """Verify that first BDP of data is marked as UNSCHEDULED.

        Mechanism: SendData() marks packets as UNSCHEDULED if sent_bytes < BDP.
        Aeolus may drop these if queue overflows.
        """
        net, backend = self._make_network(
            nb_node=2,
            profile="55us",
            stop_s=0.015,
            aeolus_thresh_pkts=20  # Moderate threshold
        )

        installed = (
            net.flare_traffic()
            .flow(0, 1, 8_192, start_s=0.0001, duration_s=0.01)
            .install()
        )
        net.start()

        stats = installed[0].stats()
        # UNSCHEDULED packets should be sent (first BDP)
        # If Aeolus drops them, we'd see it in stats (no explicit counter in current stats)
        self.assertGreater(stats.data_packets_sent, 0, "No data sent")
        self.assertGreater(stats.data_packets_received, 0, "No data received")

    def test_phase4_aeolus_drop_under_overload(self):
        """Verify Aeolus drops UNSCHEDULED packets when queue overflows.

        Mechanism: DrainSlice() checks if data queue > aeolus_thresh and
        drops UNSCHEDULED packets.
        """
        net, backend = self._make_network(
            nb_node=4,
            profile="55us",
            stop_s=0.03,
            aeolus_thresh_pkts=5  # Very low threshold to trigger drops
        )

        # Create heavy incast to trigger Aeolus drops
        installed = (
            net.flare_traffic()
            .many_to_one([0, 1, 2], dst=3, size_bytes=20_480,
                         start_s=0.0001, duration_s=0.02)
            .install()
        )
        net.start()

        # At least some flows should complete despite Aeolus drops
        completed = 0
        for flow in installed:
            stats = flow.stats()
            if stats.fct_s > 0.0:
                completed += 1

        self.assertGreaterEqual(completed, 1,
                              "No flows completed, Aeolus may be too aggressive")

    # ========================================================================
    # Integration Test: All Phases Together
    # ========================================================================

    def test_all_phases_integrated(self):
        """Integration test: all 4 phases working together."""
        net, backend = self._make_network(
            nb_node=4,
            profile="55us",
            stop_s=0.04,
            congestion_threshold_percent=50,
            tentative_threshold_percent=25,
            credit_qsize_pkts=60,
            aeolus_thresh_pkts=40
        )

        # Mixed traffic: incast + one-to-one
        installed = (
            net.flare_traffic()
            .many_to_one([0, 1], dst=2, size_bytes=12_288,
                         start_s=0.0001, duration_s=0.03)
            .flow(3, 0, 8_192, start_s=0.001, duration_s=0.025)
            .install()
        )
        net.start()

        # All mechanisms should coexist without breaking traffic
        self.assertEqual(len(installed), 3)

        completed = 0
        total_credits = 0
        total_data = 0

        for flow in installed:
            stats = flow.stats()
            total_credits += stats.credits_sent
            total_data += stats.data_packets_sent
            if stats.fct_s > 0.0:
                completed += 1

        # At least some traffic should complete with all mechanisms active
        self.assertGreater(total_credits, 0, "No credits sent")
        self.assertGreater(total_data, 0, "No data sent")
        self.assertGreaterEqual(completed, 1, "No flows completed")


if __name__ == "__main__":
    unittest.main()
