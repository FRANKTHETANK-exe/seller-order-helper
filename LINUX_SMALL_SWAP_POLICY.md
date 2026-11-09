# Linux standalone resource policy

The fixed Linux standalone profile allows 0 through 16 MiB of total allocated swap, inclusive. It is selected only when standalone mode is exactly enabled, the actual platform and measured resource platform are Linux, and controller inspection is explicitly disabled. Missing or contradictory metadata stops admission. There is no command-line override for the cap.

Sixteen MiB is at most 0.4% of the minimum 4 GiB of available memory required before a run. This small allocation allowance avoids equating every allocated swap page with a resource emergency. It is an engineering bound, not an empirically optimized threshold or proof that paging is harmless. This profile does not measure page-in/page-out rates or pressure-stall time and does not establish host/hypervisor capacity.

The cap is applied to the actual observed allocation, without subtracting a baseline or replacing the observation with zero, at admission, during monitoring with a two-second polling interval, and at post-request cleanup. An allocation above the cap retains the safe SWAP_NONZERO diagnostic code. Monitoring stops the one owned request; it never changes swap settings, kills another workload or retries automatically.

Other checks remain in force: at least 4 GiB available before loading and after cleanup, at least 40% MemAvailable throughout inference, measured unrestricted visible cgroup v2, at least four available CPUs, startup load and disk headroom, the pinned model digest, no foreign loaded model, bounded output/tokens, a 180-second Linux standalone request budget, and verified unloading with keep_alive=0. Numeric metrics must be present, finite and nonnegative; Linux available/total byte values and their percentage must agree.

Default macOS and controller profiles retain zero swap and a 60-second request budget. The pre-existing explicit StableSwapBaseline option is not repurposed: its invariant sampling and expiry remain unchanged. The original source and fallback stay separate.

Offline boundary and mocked-transport tests exercise these rules. They are not a native connected run or a claim that a generated response was reviewed or sent.
