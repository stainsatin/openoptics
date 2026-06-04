import sys
from optparse import OptionParser

class Flow:
    def __init__(self, src, dst, size, t):
        self.src, self.dst, self.size, self.t = src, dst, size, t
    def __str__(self):
        return "%d %d 3 100 %d %.9f" % (self.src, self.dst, self.size, self.t)

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-n", "--nhost", dest="nhost", help="number of hosts")
    parser.add_option("-o", "--output", dest="output", help="the output file", default="alltoall_traffic.txt")
    options, args = parser.parse_args()

    if not options.nhost:
        print "please use -n to enter number of hosts"
        sys.exit(0)
    nhost = int(options.nhost)
    output = options.output

    # flow_size 4M
    flow_size = 4 * 1024 * 1024
    start_time = 2

    flows = []
    # All-to-All
    for src in range(nhost):
        for dst in range(nhost):
            if src != dst:
                flows.append(Flow(src, dst, flow_size, start_time))

    ofile = open(output, "w")
    ofile.write("%d\n" % len(flows))
    for flow in flows:
        ofile.write(str(flow) + "\n")
    ofile.close()