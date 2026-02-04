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
Tasks are either a dedicated "instruction" (pre-programmed set of commands or other opts in the endpoint), or else the task is `"instruction" = "syscall"` and `"arg"  "some_command_to_directly_run"`

Exit code, stdout, stderr, timestamp at bottom are all set by endpoint when submitting a response.