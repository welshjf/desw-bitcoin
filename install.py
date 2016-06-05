from desw import CFG, models, ses, logger

hwb = models.HWBalance(0, 0, 'BTC', 'bitcoin')
ses.add(hwb)
try:
    ses.commit()
except Exception as ie:
    ses.rollback()
    ses.flush()

