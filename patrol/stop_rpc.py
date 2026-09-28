"""RPC confirmation only; never proof of physical stationary state.

Verified SDK 65691c8: rpc/internal.py RPC_OK=0; LocoClient.StopMove calls
SetVelocity(0., 0., 0.) (default duration 1.0) but discards its raw RPC code.
"""
RPC_SUCCESS = 0


class ResultPreservingClient:
    def __init__(self, client, rpc_ok):
        if type(rpc_ok) is not int or rpc_ok != RPC_SUCCESS:
            raise RuntimeError("unsupported SDK RPC success contract")
        self.client = client

    def Move(self, *args, **kwargs):
        return self.client.Move(*args, **kwargs)

    def StopMove(self):
        # Exactly the public call used by the verified SDK StopMove. No fallback.
        return self.client.SetVelocity(0., 0., 0.)
