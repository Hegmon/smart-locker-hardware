from app.services.backend_sync import register_device_if_needed, send_telemetry


def main() -> None:
    register_device_if_needed()
    result = send_telemetry()
    print(result)


if __name__ == "__main__":
    main()
