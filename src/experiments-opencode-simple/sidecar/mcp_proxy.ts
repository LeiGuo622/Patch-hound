import net from "node:net"

const socketPath = process.argv[2]
if (!socketPath) {
  process.stderr.write("history MCP proxy requires a Unix socket path\n")
  process.exitCode = 2
} else {
  const socket = net.createConnection(socketPath)
  socket.on("connect", () => {
    process.stdin.pipe(socket)
    socket.pipe(process.stdout)
  })
  socket.on("error", (error) => {
    process.stderr.write(`history MCP proxy failed: ${error.message}\n`)
    process.exitCode = 1
  })
  process.stdin.on("end", () => socket.end())
}

