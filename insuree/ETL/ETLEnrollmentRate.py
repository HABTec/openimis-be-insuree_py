import random , string
from sqlalchemy import text
from opensearchpy import helpers

from core.ETLBase import ETLBase

class EnrollmentRateETL(ETLBase):
    INDEX_NAME = "enrollment_rate"

    def process(self):

        offset = 1000
        rows = self._extract(offset)

        transformed = self.transform(rows)
        self.clear_index(self.INDEX_NAME)
        self.load(transformed)


    def _extract(self, offset: int):
        sql = text("""  
            SELECT
                    l."LocationId",
                    l."LocationName",
                    EXTRACT(MONTH FROM to_ethiopian_date(i."EnrollmentDate"::DATE)::date) AS month ,
                    EXTRACT(YEAR FROM to_ethiopian_date(i."EnrollmentDate"::DATE)::date) AS year ,
                    ROUND(
                        (
                            COUNT(i."InsureeID") FILTER (
                                WHERE  i."IsActive" = true
                                AND i."IsDeleted" = false
                            )::numeric
                            /
                            NULLIF(
                                COUNT(DISTINCT i."InsureeID") FILTER (
                                    WHERE i."IsHead" = true
                                ),
                                0
                            )
                        ) * 100,
                        2
                    ) AS cbhi_percentage
                FROM public."tblInsuree" i
                JOIN public."tblFamilies" f ON f."FamilyID" = i."FamilyID"
                JOIN public."tblLocations" l ON l."LocationId" = f."LocationId"
                where i."IsActive" = true
                AND i."IsDeleted" = false
                AND (i."ValidityTo" IS NULL)
                AND (f."ValidityTo" IS NULL)
                GROUP BY l."LocationId" ,
                i."EnrollmentDate"
            ;
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
                    "location_id": row.LocationId,
                    "location_name": row.LocationName,
                    "cbhi_percentage": row.cbhi_percentage,
                    "month": int(row.month),
                    "year": int(row.year),
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
