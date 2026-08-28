# CMS Bench Bring-up Guide

> **Work in progress:** This document describes the current procedure for building a new Current Measurement System (CMS) Bench. Some of these steps should eventually be automated.

---

# Hardware Requirements

A complete CMS Bench consists of:

- Raspberry Pi 4 or Raspberry Pi 5
- 2 × Wireless Pro Kits (WPKs)
- 2 × Radio Boards
- 1 × Joulescope JS220
- 1 × microSD card
- 1 × Ethernet connection

---

# Operating System Installation

The CMS Bench uses a **Raspberry Pi OS Lite** to keep the installation lightweight and suitable for headless operation, version tested is `raspi-os-trixie-2024-04-21`.

Unfortunately, due to corporate network restrictions, the Raspberry Pi Imager customization options may fail to configure the initial user account and networking.

If this happens:

- Connect a monitor and keyboard for the initial boot.
- Complete the first-boot user creation manually.
- This process should eventually be automated, since requiring physical access defeats the purpose of a remotely managed bench.

---

# Fixing Time Before Package Installation

Network restrictions prevent the Raspberry Pi from synchronizing time during the initial setup. Since `apt` refuses to update with an incorrect system time, manually set the current date first:

```bash
sudo date -s "YYYY-MM-DD HH:MM:SS"
sudo apt update
```

After networking is functioning correctly, NTP should synchronize the clock automatically.

---

# Enable SSH

Once the system is reachable on the network, enable SSH so the monitor and keyboard are no longer required.

```bash
sudo systemctl enable ssh
sudo systemctl start ssh
hostname -I
```

The reported IP address can now be used for remote access.

---

# Configure the Hostname

The IP addresses on the internal network are assigned using the DHCP service, which will probably assign a different IP address to the same CMS Bench after the next boot cycle. Giving each bench a unique hostname will avoid such unplesantries and will make sure the bench is available through a name or a particular string.

Example:

```text
cms-bench-001
cms-bench-002
cms-bench-003
```

Avoid generic hostnames that make identifying the correct bench difficult and confirm the availability of the bench number beforehand.

The hostname can be changed with:

```bash
sudo hostnamectl set-hostname cms-bench-001
```

---

# Install Simplicity Commander

To install a version of **Simplicity Commander** that is known to work correctly on the RPi5 a different kernel must be chosen to use. A known issue on RPi5 is that `aarch64` application compiled for 16KB memory pages do not run on the default RPi5 kernel, that is configured to use 4KB memory paging. The kernel image comes installed by default on any fresh installation of RPiOS on any RPi5. The only requirement is to change the system to use that kernel instead.

Before doing any changes first check the page size of the system with the `getconf PAGESIZE` which should be `4096`.

The CMS Benches are currently being tested with [Simplicity Commander 1v21p1b1949]()

> **Note**
>
> At the time of writing, the latest Commander release has page fault issues on the Raspberry Pi 5. Use the validated version documented by the project until this issue is resolved.


---

# Install Joulescope udev Rules

Download the official Joulescope udev rules:

```bash
sudo wget -O /etc/udev/rules.d/99-joulescope.rules \
https://raw.githubusercontent.com/jetperch/pyjoulescope/master/99-joulescope.rules
```

Unfortunately, the supplied rules require one modification on newer Raspberry Pi OS versions.

Locate the following line:

```text
ACTION!="add", SUBSYSTEM!="usb_device", GOTO="jlink_rules_end"
```

Replace it with:

```text
ACTION!="add", SUBSYSTEM!="usb", GOTO="jlink_rules_end"
```

Without this modification the rules will not trigger correctly on current kernels.

---

# Reload udev Rules

Reload the modified rules:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -a -G plugdev <username>
```

Reconnect the Joulescope afterwards.

Without reconnecting the device, the permissions will not be applied correctly.

---

# Configure Commander Permissions

Configure Simplicity Commander so it can access the connected debuggers without requiring elevated privileges.

> **TODO**
>
> Document the exact udev rule or installation procedure used by the project.

---

# Configure usbreset Access

The CMS software performs a **USB reset** on the Joulescope after each test.

This is intentional.

After long periods of continuous execution, the JS220 USB driver can become unresponsive. Resetting the USB device prevents the driver from hanging and avoids requiring a complete Raspberry Pi reboot.

The container therefore requires permission to execute:

```bash
usbreset
```

without requiring an interactive password.

The test process invokes `usbreset` directly. Do not install `sudo` in the
container and do not run the full CMS daemon as root merely to reset one USB
device.

For a host-native process running as `admin`, use the Joulescope udev rule above
and add `admin` to the group named by that rule (normally `plugdev`):

```bash
sudo usermod -aG plugdev admin
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Log out and back in, or restart the bench, and reconnect the Joulescope so the
new group and device-node permissions take effect.

For a container deployment, pass `/dev/bus/usb` into the container and allow
USB character devices (major number 189). A Docker invocation can use:

```bash
--mount type=bind,src=/dev/bus/usb,dst=/dev/bus/usb \
--device-cgroup-rule='c 189:* rmw'
```

If the container runs as a non-root user, also add the numeric group ID that
owns the Joulescope device node using the runtime's `--group-add`/`group_add`
setting. The udev rule remains host-owned; udev does not run inside the
application container.

Validate the exact runtime environment before starting `cmsd.py`:

```bash
usbreset 16d0:10ba
```

This command must succeed directly, without `sudo` and without a password
prompt. The Docker image already installs the `usbreset` package.

---

# Serial Port Configuration

**Never** use:

```text
/dev/ttyACM0
/dev/ttyACM1
...
```

These device names depend on enumeration order and may change after every reboot.

Instead, always access the debuggers using the persistent device paths provided by udev:

```text
/dev/serial/by-id/usb-Silicon_Labs_J-Link_Pro_OB_<JLINK_SERIAL>-if00
```

Example:

```text
/dev/serial/by-id/usb-Silicon_Labs_J-Link_Pro_OB_440365029-if00
```

This guarantees that the DUT and Tester always map to the intended hardware regardless of USB enumeration order.

This is especially important for remotely managed benches where physically identifying devices is impractical.

---

# Recommended Bench Layout

The intended CMS Bench architecture contains one of the following:

- Raspberry Pi
- Joulescope JS220
- DUT WPK
- Tester WPK
- Ethernet
- Stable power supply

Labeling the CMS Bench with the corresponding hostname should simplify remote debugging and maintenance. This document considers
unneccessary to add the cabling in the listing.

---

# Future Automation

The following tasks should eventually be automated:

- Raspberry Pi OS customization
- Initial user creation
- SSH enablement
- Hostname configuration
- Commander installation
- Joulescope installation
- udev rule installation
- usbreset permissions
- Docker installation
- Container deployment
- Bench configuration
- Local configuration generation
- Device discovery
- Health verification

The goal is for a freshly imaged Raspberry Pi to become a fully operational CMS Bench with minimal manual intervention.
