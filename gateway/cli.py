import argparse
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="Data Passport Interceptor")
    parser.add_argument("--port", type=int, default=8080, help="Port to run the interceptor on (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    print(f"Starting Data Passport Interceptor on {args.host}:{args.port}...")
    # Run the FastAPI app via Uvicorn programmatically
    uvicorn.run("gateway.app:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    main()
