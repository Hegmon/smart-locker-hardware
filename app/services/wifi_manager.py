from __future__ import annotations
import os
import shlex
import time 
import threading
from typing import Any
import subprocess
#========================================================================================
#CONFIG
#======================================================================================
DEFAULT_INTERFACE=os.getenv("WIFI_INTERFACE","wlan0")

DEFAULT_HOTSPOT_CONNECTION=os.getenv("HOTSPOT_CONNECTION","SmartLockerHotspot")
DEFAULT_HOTSPOT_SSID=os.getenv("HOTSPOT_SSID","SmartLocker-Setup")
DEFAULT_HOTSPOT_PASSWORD=os.getenv("HOTSPOT_PASSWORD","SmartLocker123")
_WIFI_LOCK=threading.Lock()

#=================================================================================
# EXCEPTION
#=============================================================================
class WifiCommandError(RuntimeError):
    pass

#==============================================================================
# SAFE EXECUTION
#==============================================================================
def _run(command:list[str],check:bool=True,timeout:int=12)->subprocess.CompletedProcess[str]:
    try:
      result=subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check
    )
    except subprocess.TimeoutExpired:
        raise WifiCommandError(f"Timeout: {' '.join(command)}")
    
    if check and result.returncode !=0:
        msg=result.stderr.strip() or result.stdout.strip()
        raise WifiCommandError(f"{' '.join(shlex.quote(c) for c in command)}: {msg}")
       
    return result

    
# =========================================================
# RETRY WRAPPER
# =========================================================
def _retry(fn,retires=2,delay=1):
    last_error=None
    for i in range(retires):
        try:
            return fn()
        except Exception as e:
            last_error=e
            time.sleep(delay)
        
    raise last_error

#=================================================================
#CORE
#=================================================================

def ensure_wifi_radio()->None:
    _run(["nmcli","radio","wifi","on"],check=False)

def _wait_for_connection(ssid:str,timeout:int=15)->bool:
    start=time.time()

    while time.time() - start < timeout:
        try:
         if get_connected_wifi_details().get("connected_ssid")==ssid:
             return True
        except Exception:
         time.sleep(1)
    return False

#==========================================================================
#STATUS
#=========================================================================
def get_wifi_status()->dict[str,Any]:
   ensure_wifi_radio()

   result=_run([
      "nmcli","-t","-f","DEVICE,STATE,CONNECTION","device","status"
   ])
   for line in result.stdout.splitlines():
      parts=line.split(":")
      
      if parts[0] != DEFAULT_INTERFACE:
         continue
      
      state=parts[1]
      connection=parts[2] if len(parts) > 2 else ""
      return {
         "interface":DEFAULT_INTERFACE,
         "state":state,
         "connected":state=="connected",
         "connection":connection if connection != "--" else "",
         "hotspot_active":connection==DEFAULT_HOTSPOT_CONNECTION
      }
   return {
      "interface":DEFAULT_INTERFACE,
      "state":"missing",
      "connected":False,
      "connection":"",
      "hotspot_active":False
   }

# =========================================================
# CONNECTED DETAILS 
# =========================================================
def get_connected_wifi_details()->dict[str,Any]:
   ensure_wifi_radio()

   data={
      "connected":False,
      "connected_ssid":"",
      "signal_strength":0,
      "is_secured":False,
   }
   try:
      result=_run([
         "nmcli",
         "-t",
         "-f",
         "GENRAL.CONNECTION,GENRAL.STATE",
         "device",
         "show",
         DEFAULT_INTERFACE

      ])
      for line in result.stdout.splitlines():
         if "GENRAL.CONNECTION" in line:
            ssid=line.split(":")[1].split()
            if ssid and ssid!="--":
               data["connected"]=True
               data["connected_ssid"]=ssid
       
      signal=_run([
           "nmcli",
           "-t",
           "-f",
           "IN-USE,SIGNAL",
           "dev",
           "wifi",
           "list",
           "ifname",
           DEFAULT_INTERFACE

        ])
      for line in signal.stdout.splitlines():
         if line.startswith("*"):
            s=int(line.split(":")[1] or 0)
            data["signal_strength"]=s
            data["rssi"]=int((s / 2) - 100)
            break
   except Exception:
      pass
      return data

# =========================================================
# HOTSPOT START
# =========================================================

def start_hotspot()->dict[str,Any]:
   def _start():
      ensure_wifi_radio()
      _run([
        "nmcli","connection","add",
        "type","wifi",
        "ifname",DEFAULT_INTERFACE,
        "con-name",DEFAULT_HOTSPOT_CONNECTION,
        "ssid",DEFAULT_HOTSPOT_SSID
      ])
      _run([
         "nmcli","connection","modify",
         DEFAULT_HOTSPOT_CONNECTION,
         "802-11-wireless.mode","ap",
         "ipv4.method","shared",
         "wifi-sec.key-mgmt","wpa-psk",
         "wifi-sec.psk",DEFAULT_HOTSPOT_PASSWORD
      ])

      result=_run(["nmcli","connection","up",DEFAULT_HOTSPOT_CONNECTION])
      return {
         "status":"hotspot_enabled",
          "ssid":DEFAULT_HOTSPOT_SSID,
           "details":result.stdout.strip()
           }
   with _WIFI_LOCK:
      return _retry(_start)


def stop_hotspot()->None:
   _run(["nmcli","connection","down",DEFAULT_HOTSPOT_CONNECTION],check=False)


# =========================================================
# RECONNECT 
# =========================================================
def reconnect_saved_wifi(ssid:str)->dict[str,Any]:
   def _connect():
      ensure_wifi_radio()

      result=_run([
         "nmcli","dev","wifi","connect",ssid,
         "ifname",DEFAULT_INTERFACE
      ])
      if not _wait_for_connection(ssid):
         raise WifiCommandError(f"Reconnect failed:{ssid}")
      return {
         "status":"reconnected",
         "ssid":ssid,
         "details":result.stdout.strip(),
         "connection":get_connected_wifi_details()
      }
   with _WIFI_LOCK:
      return _retry(_connect)
   

# =========================================================
# CONNECT WIFI 
# =========================================================
def connect_wifi(ssid:str,password:str)->dict[str,Any]:
   def _connect():
      ensure_wifi_radio()

      stop_hotspot()
      time.sleep(1)
      _run(["nmcli","connection","delete",ssid],check=False)

      cmd=["nmcli","dev","wifi","connect",ssid,"ifname",DEFAULT_INTERFACE]
      if password:
         cmd+=["password",password]
      result=_run(cmd)
      if not _wait_for_connection(ssid):
         raise WifiCommandError(f"Connection failed:{ssid}")
      return{
         "status":"connected",
         "ssid":ssid,
         "details":result.stdout.strip(),
         "connection":get_connected_wifi_details()
      }
   with _WIFI_LOCK:
      return _retry(_connect)
   
#=================================================================================
#DISCONNECT 
#================================================================================
def disconnect_wifi()->dict[str,Any]:
   with _WIFI_LOCK:
      _run(["nmcli","device","disconnect",DEFAULT_INTERFACE],check=False)
      hotspot=start_hotspot()
      return {
         "status":"disconnected",
         "hotspot":hotspot
      }
   
#======================================================================================
# HEALTH CHECK
#=====================================================================================
def is_wifi_connectd()->bool:
   status=get_wifi_status()
   return status["connected"] and not status["hotspot_active"]