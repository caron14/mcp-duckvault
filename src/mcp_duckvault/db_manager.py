import duckdb
import logging

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path: str = "vault.db"):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Establish a connection to DuckDB and ensure the vss extension is loaded."""
        logger.info(f"Connecting to DuckDB at {self.db_path}")
        self.conn = duckdb.connect(self.db_path)
        
        # Install and load vss extension
        self.conn.execute("INSTALL vss;")
        self.conn.execute("LOAD vss;")
        logger.info("vss extension loaded successfully")

    def initialize_schema(self):
        """Create the necessary tables if they don't exist."""
        if not self.conn:
            self.connect()

        logger.info("Initializing database schema...")
        
        # Table for system configuration and sync status
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key VARCHAR PRIMARY KEY,
                value VARCHAR
            );
        """)

        # Table for document metadata
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                path VARCHAR PRIMARY KEY,
                md5 VARCHAR,
                metadata JSON,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Table for text chunks with embeddings
        # document_path REFERENCES documents(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id VARCHAR PRIMARY KEY,
                document_path VARCHAR,
                content TEXT,
                embedding FLOAT[],
                metadata JSON,
                FOREIGN KEY (document_path) REFERENCES documents(path)
            );
        """)

        # Create HNSW index for vector search if it doesn't exist
        # Note: In DuckDB vss, indexes are created on the embedding column.
        # We'll check if index exists or just try/catch
        try:
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS chunk_vec_idx ON chunks USING HNSW (embedding) WITH (metric = 'cosine');
            """)
            logger.info("HNSW index created/verified")
        except Exception as e:
            logger.warning(f"Could not create HNSW index: {e}. Vector search might be slower.")

        logger.info("Schema initialization complete")

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
