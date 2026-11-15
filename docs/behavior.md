# Behavior and limitations

## Devices and mappings

One integration entry manages multiple virtual devices. Each virtual device has
one physical source; a physical source can serve one virtual device at a time.
Assigning an occupied source unassigns its previous virtual device while keeping
that device's entities, history, and totals.

Mappings are saved for each virtual/physical device pair. New matches are proposed
only when domain, device class, state class, and compatible units give an
unambiguous match. Names and list order do not determine a match. Uncertain
mappings require a manual choice. Every assignment ends with a review and explicit
confirmation; closing the wizard leaves the current assignment active.

Existing readings keep their IDs, units, types, and handling modes. Compatible
source units are converted before values or counter differences are used.
Unmapped readings retain their history. Extra readings and on/off controls can be
added through the reassignment wizard. Removing individual virtual devices or
readings is not currently supported.

## Values and updates

Mirrored entities show their source's latest valid Home Assistant value, including
unchanged zero readings. Updates follow source state events; there is no separate
polling interval. Source integrations determine report frequency. A cached value
can predate assignment; `source_last_updated` shows its timestamp.

Unassigned, unmapped, disabled, incompatible, and unavailable sources make mirrored
entities unavailable. Allocated totals remain visible and hold their last value.
Sources are tracked by entity registry identity, so renaming an entity works and
an unrelated entity reusing its old entity ID does not inherit the mapping.

Numeric, text, date, timestamp, enum, and binary values are supported. Enum sources
must fit the virtual sensor's existing options. Raw source history is not moved
or rewritten; virtual history starts when its entities are created.

## Counter modes

| Mode | Behavior |
| --- | --- |
| Mirror the current reading | Copies the source value, including a raw counter's lifetime value. |
| Allocate counter increases | Adds positive cumulative differences to an independent total. Output uses `total_increasing`. |
| Allocate signed counter changes | Adds signed cumulative differences, including decreases and negative values. Output uses `total`. |

Allocation is for cumulative counters, not interval usage or live measurements.
Available modes follow Home Assistant's allowed state classes. Each allocated
reading has an independent checkpoint, so several counters can share a device.

For example, a plug rising from 42.1 to 42.3 kWh adds 0.2 kWh to its assigned
virtual device. Reassigning routes later increments elsewhere. Returning to the
first device resumes its previous total.

Stop the old appliance and wait for its final report before moving the source.
The integration uses the latest valid counter when assignment is confirmed; it
cannot split a delayed report that spans a move or force a hardware reading.
If a counter is unavailable or its reset is uncertain at handover, the first valid
reading establishes a baseline and the unknown interval is discarded. An unchanged
assignment keeps its baseline through an outage and recovers use on a later report.

In increase mode, a decreased counter needs a second low, nondecreasing report to
confirm a reset. Counting resumes from the first low reading. A single false zero
followed by the original counter does not count the lifetime total again. A changed
explicit `last_reset` marker establishes a known reset boundary. Signed allocation
respects explicit reset markers but does not infer resets from decreases.

## On/off controls

Switches are optional and unselected by default. To add one to an existing virtual
device, reassign it to its current source, select the switch under **Additional
readings and optional controls**, then review and confirm.

Commands go to the currently mapped physical switch. Displayed state follows the
source rather than assuming success. Unavailable or unmapped sources cannot
receive proxy commands. A command in progress finishes before a new assignment
is applied. Lights, climate entities, buttons, and number controls are not forwarded.

## Storage and backups

Assignments, mappings, reading definitions, and counters are saved together in
Home Assistant's `.storage` directory before a confirmed change completes. Failed
saves keep the previous assignment. Counter checkpoints are saved within five
seconds of the first pending update and at normal shutdown; this timer does not
delay live readings.

Include `.storage` in Home Assistant backups. Abrupt power loss can lose recent
unsaved data; surviving cumulative source counters normally recover the interval.
A missing or invalid established checkpoint stops setup instead of resetting totals.
Removing the integration removes its checkpoint. Use Rename or Unassign when you
want to preserve history.
