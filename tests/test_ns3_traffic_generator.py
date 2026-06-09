import os
import unittest
from unittest.mock import patch

from openoptics.backends.ns3.traffic import (
    FlareConfig,
    FlareStats,
    FlareTrafficGenerator,
    TcpTrafficGenerator,
    UdpTrafficGenerator,
    parse_bitrate,
)
try:
    from ns3_helpers import ns3_available, skip_if_no_ns3
except ModuleNotFoundError:  # unittest module mode: tests.test_...
    from tests.ns3_helpers import ns3_available, skip_if_no_ns3


class RecordingBackend:
    def __init__(self, nb_node=4, simulation_stop_s=1.0):
        self._nb_node = nb_node
        self._simulation_stop_s = simulation_stop_s
        self._next_traffic_port = 9000
        self._next_flare_flow_id = 1
        self.calls = []

    def _allocate_traffic_port(self):
        port = self._next_traffic_port
        self._next_traffic_port += 1
        return port

    def _allocate_flare_flow_id(self):
        flow_id = self._next_flare_flow_id
        self._next_flare_flow_id += 1
        return flow_id

    def install_udp_flow(self, **kwargs):
        self.calls.append(("udp", kwargs))
        return ("udp-server", "udp-client")

    def install_tcp_bulk_flow(self, **kwargs):
        self.calls.append(("tcp-bulk", kwargs))
        return ("tcp-sink", "tcp-source")

    def install_onoff_flow(self, **kwargs):
        self.calls.append(("onoff", kwargs))
        return ("sink", "source")

    def install_udp_echo_flow(self, **kwargs):
        self.calls.append(("echo", kwargs))
        return ("echo-server", "echo-client")

    def install_flare_flow(self, **kwargs):
        self.calls.append(("flare", kwargs))
        return ("flare-src", "flare-dst")

    def flare_stats_for(self, spec):
        return FlareStats(
            flow_id=spec.flow_id,
            src=spec.src,
            dst=spec.dst,
            fct_s=0.001,
            throughput_bps=spec.size_bytes * 8 / 0.001,
            data_packets_sent=spec.num_packets,
            data_packets_received=spec.num_packets,
            credits_sent=spec.num_packets,
            credits_received=spec.num_packets,
        )


class UdpTrafficGeneratorTests(unittest.TestCase):
    def test_parse_bitrate_units(self):
        self.assertEqual(parse_bitrate("10Mbps"), 10_000_000)
        self.assertEqual(parse_bitrate("1G"), 1_000_000_000)
        self.assertEqual(parse_bitrate(500, default_unit="Kbps"), 500_000)

    def test_rate_and_duration_compile_to_packet_schedule(self):
        backend = RecordingBackend()

        installed = (
            UdpTrafficGenerator(backend)
            .flow(
                0,
                1,
                rate="8Mbps",
                start_s=0,
                duration_s=0.01,
                packet_size_bytes=1000,
            )
            .install()
        )

        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0].spec.port, 9000)
        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "udp")
        self.assertEqual(kwargs["src"], 0)
        self.assertEqual(kwargs["dst"], 1)
        self.assertEqual(kwargs["num_packets"], 10)
        self.assertAlmostEqual(kwargs["interval_s"], 0.001)
        self.assertEqual(kwargs["port"], 9000)

    def test_common_patterns_assign_unique_ports(self):
        backend = RecordingBackend()

        (
            UdpTrafficGenerator(backend)
            .bidirectional(0, 1, num_packets=2, interval_s=0.1)
            .many_to_one([0, 1, 2, 3], 3, num_packets=1, interval_s=0.1)
            .install()
        )

        self.assertEqual(
            [(mode, call["src"], call["dst"]) for mode, call in backend.calls],
            [
                ("udp", 0, 1),
                ("udp", 1, 0),
                ("udp", 0, 3),
                ("udp", 1, 3),
                ("udp", 2, 3),
            ],
        )
        self.assertEqual(
            [call["port"] for _mode, call in backend.calls],
            [9000, 9001, 9002, 9003, 9004],
        )

    def test_from_matrix_uses_rate_values(self):
        backend = RecordingBackend()
        matrix = {
            (0, 1): "10Mbps",
            (1, 1): 99,          # skipped self-flow
            (2, 3): "500Kbps",
            (3, 0): 0,           # skipped zero
        }

        (
            UdpTrafficGenerator(backend)
            .from_matrix(
                matrix,
                start_s=0,
                duration_s=0.01,
                packet_size_bytes=1000,
            )
            .install()
        )

        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(
            (backend.calls[0][1]["src"], backend.calls[0][1]["dst"]),
            (0, 1),
        )
        self.assertAlmostEqual(backend.calls[0][1]["interval_s"], 0.0008)
        self.assertEqual(
            (backend.calls[1][1]["src"], backend.calls[1][1]["dst"]),
            (2, 3),
        )
        self.assertAlmostEqual(backend.calls[1][1]["interval_s"], 0.016)

    def test_echo_uses_echo_installer(self):
        backend = RecordingBackend()

        (
            UdpTrafficGenerator(backend)
            .echo(0, 1, num_packets=1, interval_s=0.1)
            .install()
        )

        self.assertEqual(backend.calls[0][0], "echo")

    def test_validation_errors_are_early(self):
        backend = RecordingBackend()
        gen = UdpTrafficGenerator(backend)

        with self.assertRaises(ValueError):
            gen.flow(0, 0)
        with self.assertRaises(IndexError):
            gen.flow(0, 10)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, rate="1Mbps", packets_per_second=100)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, rate="1Mbps", interval_s=0.01)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, packets_per_second=100, interval_s=0.01)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, stop_s=0.2, duration_s=0.15)
        with self.assertRaises(ValueError):
            gen.from_matrix({(0, 1): "1Mbps"}, interval_s=0.01)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, rate="1bananas")

    def test_udp_size_bytes_derives_packet_count(self):
        backend = RecordingBackend()

        installed = (
            UdpTrafficGenerator(backend)
            .flow(
                0,
                1,
                size_bytes=2501,
                packet_size_bytes=1000,
                interval_s=0.1,
            )
            .install()
        )

        self.assertEqual(installed[0].spec.protocol, "udp")
        self.assertEqual(installed[0].spec.mode, "client-server")
        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "udp")
        self.assertEqual(kwargs["num_packets"], 3)
        self.assertEqual(kwargs["packet_size_bytes"], 1000)
        self.assertAlmostEqual(kwargs["stop_s"], 0.35)

    def test_udp_onoff_is_supported(self):
        backend = RecordingBackend()
        gen = UdpTrafficGenerator(backend).onoff(
            0,
            1,
            rate="5Mbps",
            size_bytes=1_000,
            duration_s=0.5,
        )

        desc = gen.describe()[0]
        self.assertEqual(desc["size_bytes"], 1_000)
        self.assertNotIn("max_bytes", desc)
        gen.install()

        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "onoff")
        self.assertEqual(kwargs["protocol"], "udp")
        self.assertEqual(kwargs["rate_bps"], 5_000_000)
        self.assertEqual(kwargs["max_bytes"], 1_000)

    def test_size_bytes_and_num_packets_conflict(self):
        backend = RecordingBackend()

        with self.assertRaises(ValueError):
            UdpTrafficGenerator(backend).flow(
                0,
                1,
                size_bytes=1000,
                num_packets=10,
            )

    def test_describe_reflects_allocated_port_after_install(self):
        backend = RecordingBackend()
        gen = UdpTrafficGenerator(backend).flow(
            0, 1, num_packets=1, interval_s=0.1,
        )

        self.assertIsNone(gen.describe()[0]["port"])
        gen.install()
        self.assertEqual(gen.describe()[0]["port"], 9000)

        with self.assertRaises(RuntimeError):
            gen.install()
        with self.assertRaises(RuntimeError):
            gen.flow(1, 2)


class TcpTrafficGeneratorTests(unittest.TestCase):
    def test_tcp_bulk_dispatches_total_size(self):
        backend = RecordingBackend()
        gen = TcpTrafficGenerator(backend).bulk(
            0,
            1,
            size_bytes=10_000,
            chunk_size_bytes=1448,
        )

        self.assertNotIn("max_bytes", gen.describe()[0])
        self.assertEqual(gen.describe()[0]["chunk_size_bytes"], 1448)
        installed = gen.install()

        self.assertEqual(installed[0].spec.protocol, "tcp")
        self.assertEqual(installed[0].spec.mode, "bulk")
        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "tcp-bulk")
        self.assertEqual(kwargs["max_bytes"], 10_000)
        self.assertEqual(kwargs["send_size_bytes"], 1448)

    def test_tcp_onoff_dispatches_rate_and_size(self):
        backend = RecordingBackend()

        (
            TcpTrafficGenerator(backend)
            .onoff(
                0,
                1,
                rate="100Mbps",
                size_bytes=10_000,
                packet_size_bytes=1448,
                duration_s=1.0,
            )
            .install()
        )

        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "onoff")
        self.assertEqual(kwargs["protocol"], "tcp")
        self.assertEqual(kwargs["max_bytes"], 10_000)
        self.assertEqual(kwargs["packet_size_bytes"], 1448)
        self.assertEqual(kwargs["rate_bps"], 100_000_000)

    def test_tcp_bulk_validation_errors_are_early(self):
        backend = RecordingBackend()

        with self.assertRaises(ValueError):
            TcpTrafficGenerator(backend).bulk(0, 0)
        with self.assertRaises(IndexError):
            TcpTrafficGenerator(backend).bulk(0, 10)
        with self.assertRaises(ValueError):
            TcpTrafficGenerator(backend).bulk(0, 1, size_bytes=0)
        with self.assertRaises(ValueError):
            TcpTrafficGenerator(backend).bulk(0, 1, chunk_size_bytes=0)


class FlareTrafficGeneratorTests(unittest.TestCase):
    def test_profiles_match_paper_defaults(self):
        p55 = FlareConfig.from_profile("55us")
        self.assertEqual(p55.credit_qsize_pkts, 60)
        self.assertEqual(p55.shaping_thresh_pkts, 30)
        self.assertEqual(p55.aeolus_thresh_pkts, 40)
        self.assertEqual(p55.w_init, 1.0)
        self.assertEqual(p55.target_loss, 0.1)

        p15 = FlareConfig.from_profile("15us")
        self.assertEqual(p15.credit_qsize_pkts, 16)
        self.assertEqual(p15.shaping_thresh_pkts, 8)
        self.assertEqual(p15.aeolus_thresh_pkts, 8)

    def test_flare_flow_installs_credit_based_spec(self):
        backend = RecordingBackend()
        cfg = FlareConfig.from_profile("15us", mtu_bytes=512)

        installed = (
            FlareTrafficGenerator(backend, config=cfg)
            .flow(0, 1, 1500, start_s=0.01, duration_s=0.1)
            .install()
        )

        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0].spec.protocol, "flare")
        self.assertEqual(installed[0].spec.flow_id, 1)
        self.assertEqual(installed[0].spec.port, 9000)
        self.assertEqual(installed[0].spec.num_packets, 3)
        mode, kwargs = backend.calls[0]
        self.assertEqual(mode, "flare")
        self.assertEqual(kwargs["src"], 0)
        self.assertEqual(kwargs["dst"], 1)
        self.assertEqual(kwargs["flow_id"], 1)
        self.assertEqual(kwargs["size_bytes"], 1500)
        self.assertEqual(kwargs["packet_size_bytes"], 512)
        self.assertEqual(kwargs["config"].profile, "15us")

    def test_flare_builder_defaults_are_applied(self):
        backend = RecordingBackend()
        gen = FlareTrafficGenerator(
            backend,
            start_s=0.2,
            duration_s=0.3,
            packet_size_bytes=256,
            path_id=7,
        )

        gen.flow(0, 1, 1024).install()

        _mode, kwargs = backend.calls[0]
        self.assertEqual(kwargs["start_s"], 0.2)
        self.assertEqual(kwargs["stop_s"], 0.5)
        self.assertEqual(kwargs["packet_size_bytes"], 256)
        self.assertEqual(kwargs["path_id"], 7)

    def test_flare_common_patterns_assign_flow_ids_and_ports(self):
        backend = RecordingBackend()

        installed = (
            FlareTrafficGenerator(backend)
            .many_to_one([0, 1, 2], 2, 2048)
            .install()
        )

        self.assertEqual(
            [(call["flow_id"], call["port"], call["src"], call["dst"])
             for _mode, call in backend.calls],
            [(1, 9000, 0, 2), (2, 9001, 1, 2)],
        )
        stats = installed[0].stats()
        self.assertIsInstance(stats, FlareStats)
        self.assertEqual(stats.credits_sent, installed[0].spec.num_packets)

    def test_flare_validation_errors_are_early(self):
        backend = RecordingBackend()
        gen = FlareTrafficGenerator(backend)

        with self.assertRaises(ValueError):
            gen.flow(0, 0, 1000)
        with self.assertRaises(IndexError):
            gen.flow(0, 99, 1000)
        with self.assertRaises(ValueError):
            gen.flow(0, 1, 0)
        with self.assertRaises(ValueError):
            FlareConfig.from_profile("missing")
        with self.assertRaises(ValueError):
            FlareConfig.from_profile("55us", target_loss=1.5)


@skip_if_no_ns3
class TrafficGeneratorNs3EndToEndTests(unittest.TestCase):
    def tearDown(self):
        if ns3_available():
            from ns import ns
            ns.Simulator.Destroy()

    def test_one_way_udp_builder_delivers_without_echo_reply(self):
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend

        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"
        try:
            backend = Ns3Backend()
            with patch("openoptics.Toolbox.create_backend", return_value=backend):
                net = Toolbox.BaseNetwork(
                    name="traffic_builder_one_way",
                    backend="ns3",
                    nb_node=4,
                    time_slice_duration_us=10_000,
                    guardband_ms=0,
                    use_webserver=False,
                    simulation_stop_s=0.3,
                )
                net.deploy_topo(OpticalTopo.round_robin(nb_node=4))
                net.deploy_routing(
                    OpticalRouting.routing_direct(net.get_topo()),
                    routing_mode="Per-hop",
                )
                backend.udp_traffic() \
                    .flow(0, 1, start_s=0.01, stop_s=0.25,
                          num_packets=5, interval_s=0.02) \
                    .install()
                net.start()

            self.assertEqual(backend._tor_apps[0].GetIngressFromHostCount(), 5)
            self.assertEqual(backend._tor_apps[1].GetDeliveredToHostCount(), 5)
            self.assertEqual(backend._tor_apps[0].GetDeliveredToHostCount(), 0)
            backend.cleanup()
        finally:
            os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
            os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)

    def test_installed_traffic_stats_returns_flow_stats(self):
        """``InstalledTraffic.stats()`` exposes FlowMonitor's per-flow data
        for a one-way UDP flow, with the 5-tuple matching the spec.
        """
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend
        from openoptics.backends.ns3.traffic import FlowStats

        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"
        try:
            backend = Ns3Backend()
            with patch("openoptics.Toolbox.create_backend", return_value=backend):
                net = Toolbox.BaseNetwork(
                    name="flow_stats_one_way",
                    backend="ns3",
                    nb_node=4,
                    time_slice_duration_us=10_000,
                    guardband_ms=0,
                    use_webserver=False,
                    simulation_stop_s=0.3,
                )
                net.deploy_topo(OpticalTopo.round_robin(nb_node=4))
                net.deploy_routing(
                    OpticalRouting.routing_direct(net.get_topo()),
                    routing_mode="Per-hop",
                )
                installed = (
                    backend.udp_traffic()
                    .flow(0, 1, start_s=0.01, stop_s=0.25,
                          num_packets=5, interval_s=0.02)
                    .install()
                )
                net.start()

            self.assertEqual(len(installed), 1)
            stats = installed[0].stats()
            self.assertIsNotNone(stats, "expected FlowStats after run")
            self.assertIsInstance(stats, FlowStats)
            self.assertEqual(stats.protocol, "udp")
            self.assertEqual(stats.dst_port, installed[0].spec.port)
            self.assertEqual(stats.tx_packets, 5)
            self.assertEqual(stats.rx_packets, 5)
            self.assertEqual(stats.lost_packets, 0)
            self.assertGreater(stats.delay_avg_s, 0.0)
            self.assertGreater(stats.fct_s, 0.0)
            backend.cleanup()
        finally:
            os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
            os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)

    def test_get_flow_stats_disambiguates_concurrent_flows(self):
        """With multiple flows sharing a destination, each
        ``InstalledTraffic.stats()`` resolves to the right flow via the
        per-flow port allocation.
        """
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend

        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"
        try:
            backend = Ns3Backend()
            with patch("openoptics.Toolbox.create_backend", return_value=backend):
                net = Toolbox.BaseNetwork(
                    name="flow_stats_many_to_one",
                    backend="ns3",
                    nb_node=4,
                    time_slice_duration_us=10_000,
                    guardband_ms=0,
                    use_webserver=False,
                    simulation_stop_s=0.3,
                )
                net.deploy_topo(OpticalTopo.round_robin(nb_node=4))
                net.deploy_routing(
                    OpticalRouting.routing_direct(net.get_topo()),
                    routing_mode="Per-hop",
                )
                installed = (
                    backend.udp_traffic()
                    .many_to_one([0, 2, 3], dst=1, start_s=0.01,
                                 stop_s=0.25, num_packets=3, interval_s=0.02)
                    .install()
                )
                net.start()

            self.assertEqual(len(installed), 3)
            ports = {inst.spec.port for inst in installed}
            self.assertEqual(len(ports), 3, "ports must be unique")
            for inst in installed:
                stats = inst.stats()
                self.assertIsNotNone(stats)
                self.assertEqual(stats.dst_port, inst.spec.port)
                self.assertEqual(stats.tx_packets, 3)
                self.assertEqual(stats.rx_packets, 3)
            self.assertEqual(len(backend.get_flow_stats()), 3)
            backend.cleanup()
        finally:
            os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
            os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)

    def test_tcp_bulk_builder_delivers(self):
        from openoptics import OpticalRouting, OpticalTopo, Toolbox
        from openoptics.backends.ns3.backend import Ns3Backend

        os.environ["OPENOPTICS_NS3_NO_PAUSE"] = "1"
        os.environ["OPENOPTICS_NS3_NO_REPORT"] = "1"
        try:
            backend = Ns3Backend()
            with patch("openoptics.Toolbox.create_backend", return_value=backend):
                net = Toolbox.BaseNetwork(
                    name="traffic_builder_tcp_bulk",
                    backend="ns3",
                    nb_node=4,
                    time_slice_duration_us=10_000,
                    guardband_ms=0,
                    use_webserver=False,
                    simulation_stop_s=0.5,
                )
                net.deploy_topo(OpticalTopo.round_robin(nb_node=4))
                net.deploy_routing(
                    OpticalRouting.routing_direct(net.get_topo()),
                    routing_mode="Per-hop",
                )
                backend.tcp_traffic() \
                    .bulk(0, 1, size_bytes=4096, start_s=0.01,
                          stop_s=0.45) \
                    .install()
                net.start()

            self.assertGreaterEqual(
                backend._tor_apps[1].GetDeliveredToHostCount(), 1
            )
            backend.cleanup()
        finally:
            os.environ.pop("OPENOPTICS_NS3_NO_REPORT", None)
            os.environ.pop("OPENOPTICS_NS3_NO_PAUSE", None)


if __name__ == "__main__":
    unittest.main()
