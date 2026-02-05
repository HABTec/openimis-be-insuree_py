import random , string
from sqlalchemy import text
from opensearchpy import helpers

from core.ETLBase import ETLBase

class TotalMoneyCollectedETL(ETLBase):
    INDEX_NAME = "total_money_collected"

    def process(self):

        offset = 1000
        rows = self._extract(offset)

        transformed = self.transform(rows)
        self.clear_index(self.INDEX_NAME)
        self.load(transformed)


    def _extract(self, offset: int):
        sql = text("""
            WITH valid_payments AS (
                SELECT
                    pay."PaymentID",
                    pay."ReceivedAmount",
                    pay."PaymentDate",
                    pay."ReceivedDate",
                    pd."PremiumID",
                    pd."Amount"           AS payment_detail_amount,
                    pd."ProductCode",
                    pd."PolicyStage",
                    pr."PremiumId",
                    pr."PolicyID",
                    pr."Amount"           AS premium_amount,
                    p."FamilyID",
                    f."LocationId"
                FROM public."tblPaymentDetails" pd
                JOIN public."tblPayment" pay
                    ON pay."PaymentID" = pd."PaymentID"
                JOIN public."tblPremium" pr
                    ON pr."PremiumId" = pd."PremiumID"
                JOIN public."tblPolicy" p
                    ON p."PolicyID" = pr."PolicyID"
                JOIN public."tblFamilies" f
                    ON f."FamilyID" = p."FamilyID"
                WHERE
                    pay."ReceivedAmount" IS NOT NULL
                    AND pay."ReceivedAmount" > 0
            )
            SELECT
                vp."LocationId",
                EXTRACT(MONTH FROM to_ethiopian_date(vp."ReceivedDate"::DATE)::date) AS month ,
                EXTRACT(YEAR FROM to_ethiopian_date(vp."ReceivedDate"::DATE)::date) AS year ,
                SUM(vp."ReceivedAmount") AS total_amount_collected_etb
            FROM valid_payments vp
            GROUP BY
                vp."LocationId",
                vp."ReceivedDate"
            ORDER BY
                vp."LocationId";
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
                    "total_amount_collected_etb": row.total_amount_collected_etb,
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
