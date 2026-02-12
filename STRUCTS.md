# JSON Structs for Used objects

## Host Info
```json
{
  "hostname": "string hostname",
  "ip_address": "host ip",
  "os": "os family <linux/windows>",
  "os_name": "os type <win10/win11/distro_name etc>",
  "registered_at": "timestamp",
  "last_seen": "timestamp",
  "tasks": []
}
```

## Tasks
```json
{
  "task_id": "blaaaah",
  "assigned_at": "TIMESTAMP_HERE",
  "instruction": "XYZ",
  "arg": "command or nil",
  "exit_code": null,
  "stdout": "",
  "stderr": "",
  "stopped_processing_at": "TIMESTAMP_HERE",
  "responded": false
}
```
### Task explanation
Tasks are either a dedicated "instruction" (pre-programmed set of commands or other opts in the endpoint), or else the task is `"instruction" = "syscall"` and `"arg" = "some_command_to_directly_run"`

Exit code, stdout, stderr, timestamp at bottom are all set by endpoint when submitting a response. (and are ONLY guaranteed for `syscall` tasks)


## Task TYPE reference
* `syscall` - run system command `arg`
* `exit` - quit endpoint agent (no args)
* `inventory` - returns a detailed "healthcheck" JSON with OS info / version, CPU cores, memory in use, and disk usage info (no args)
  * Output is in `"inventory"` which is not typically a part of the Task JSON struct
* TODO: respawn

### Schema enforcement
- Clients may omit response-only fields (`exit_code`, `stdout`, `stderr`, `stopped_processing_at`, `responded`, `inventory`) when creating tasks; the backend fills them using `_TASK_DEFAULTS`.
- `arg` should be an empty string (`""`) when the instruction does not require parameters—`null` will be normalized but may trigger validation warnings.
- The backend enforces a strict allowlist via `EndpointDatabase._TASK_FIELDS`. Any new fields must be added there (and to `_TASK_DEFAULTS`) or the data will be stripped before persistence.
