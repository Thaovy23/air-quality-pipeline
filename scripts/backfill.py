"""backfill.py - Load historical air quality data from JSON files into Postgres."""
import os
import json
from dotenv import load_dotenv

load_dotenv()

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")


def backfill(json_file: str):
    """Read a JSON export file and insert rows into Postgres."""
        # TODO: implement backfill logic from 2 source JSON files
            with open(json_file) as f:
                    data = json.load(f)
                        pass


                        if __name__ == "__main__":
                            import sys
                                if len(sys.argv) < 2:
                                        print("Usage: python backfill.py <path-to-json>")
                                                sys.exit(1)
                                                    backfill(sys.argv[1])
                                                    
