def get_device_name():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startwith("Serial"):
                   serial=line.split(":")[1].strip()[-6:]
                   return f"SmartLocker-{serial}"
    except:
        pass
    
    return "SmartLocker-UNKNOWN"