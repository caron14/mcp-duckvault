import logging

import duckdb

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

        # Enable experimental HNSW persistence for persistent databases
        # This must be done AFTER loading the vss extension
        try:
            self.conn.execute("SET hnsw_enable_experimental_persistence = true;")
        except Exception as e:
            logger.debug(f"Could not set hnsw_enable_experimental_persistence: {e}")

    def initialize_schema(self, embedding_dim: int = 384):
        """
        Create the necessary tables if they don't exist.
        
        Args:
            embedding_dim (int): The dimension of the vector embeddings.
                                Defaults to 384 (multilingual-e5-small).
        """
        if not self.conn:
            self.connect()

        logger.info(f"Initializing database schema with embedding_dim={embedding_dim}...")

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
        # Note: HNSW index requires a fixed-size FLOAT array (FLOAT[N])
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id VARCHAR PRIMARY KEY,
                document_path VARCHAR,
                content TEXT,
                embedding FLOAT[{embedding_dim}],
                metadata JSON
            );
        """)

        # Create HNSW index for vector search if it doesn't exist
        try:
            # First, check if index exists in DuckDB system tables
            existing = self.conn.execute("SELECT * FROM duckdb_indexes() WHERE index_name = 'chunk_vec_idx'").fetchone()
            if not existing:
                self.conn.execute("""
                    CREATE INDEX chunk_vec_idx ON chunks USING HNSW (embedding) WITH (metric = 'cosine');
                """)
                logger.info("HNSW index created successfully")
            else:
                logger.info("HNSW index already exists")
        except Exception as e:
            logger.warning(f"Could not create HNSW index: {e}. Vector search might be slower.")

        logger.info("Schema initialization complete")

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")
