"""Run one local service process; its browser and resume belong to this process."""
import uvicorn


def main():
    uvicorn.run("sww.api:app", host="127.0.0.1", port=8765, workers=1)


if __name__ == "__main__":
    main()
