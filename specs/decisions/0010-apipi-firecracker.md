# 0010. ApiPi owns Firecracker

ApiPi starts Firecracker itself. Spawn, TAP, vsock, workspace
pack/unpack, and guest init stay in this repo. Jailer is useful when
present; it is not required.

Flintlock, firecracker-containerd, and Kata remain possible later if
they prove a clear win. They are not the path we ship now. They add
daemons (flintlockd, containerd, or a cluster) while guest RPC,
workspace, and session lifecycle still belong to ApiPi.

When the API and sandbox workers split, Firecracker stays on the
worker, still started by ApiPi.
