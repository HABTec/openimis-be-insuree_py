import random , string
from sqlalchemy import text
from opensearchpy import helpers

from core.ETLBase import ETLBase

class TotalCBHIMemberAndBeneficiaryETL(ETLBase):
    INDEX_NAME = "total_cbhi_member_and_beneficiary"

    def process(self):

        offset = 1000
        rows = self._extract(offset)

        transformed = self.transform(rows)
        self.clear_index(self.INDEX_NAME)
        self.load(transformed)


    def _extract(self, offset: int):
        sql = text("""
            SELECT
                COUNT(DISTINCT CASE WHEN i."IsHead" = TRUE  THEN i."InsureeID" END) AS family_heads,
                COUNT(DISTINCT CASE WHEN i."IsHead" = FALSE THEN i."InsureeID" END) AS beneficiaries,
                f."LocationId" 
            FROM "tblPolicy" p
            JOIN "tblFamilies" f
                ON f."FamilyID" = p."FamilyID"
            JOIN "tblInsuree" i
                ON i."FamilyID" = f."FamilyID"
            WHERE p."PolicyStatus" = 2       
            AND p."ValidityTo" is null
            AND i."status" = 'AC'
            group by f."LocationId" ;
        """)
        conn = self.engine.connect()
        result = conn.execute(
            sql,
            # {
            #     "limit": self.BATCH_SIZE,
            #     "offset": offset,
            # }
        )
        res = result.mappings().all()
        return res
     
    def transform(self, rows):
        docs = []

        for row in rows:
            docs.append({
                "_index": self.INDEX_NAME,
                "_source": {
                    "family_heads": row.family_heads,
                    "beneficiaries": row.beneficiaries,
                    "location_id": row.LocationId,
                }
            })

        return docs

    def load(self, docs):
        if not docs:
            return

        success, failed = helpers.bulk(
            client=self.opensearch,
            actions=docs,
            stats_only=True,
        )

        print("Success:", success)
        print("Failed:", failed)
