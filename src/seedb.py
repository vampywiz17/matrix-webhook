import sqlite3

# Connect to the database
conn = sqlite3.connect('path-to-db-file.db')

# Create a cursor object
cursor = conn.cursor()

# Define the new session ID

# # Define the update statement to set the session_id of the first record
# update_statement = """
#     UPDATE megolminboundsessions
#     SET session_id = ?
#     WHERE rowid = (
#         SELECT rowid
#         FROM megolminboundsessions
#         LIMIT 1 
#     );
# """

# # Execute the update statement
# cursor.execute(update_statement, (new_session_id,))

# # Commit the transaction to save the changes
# conn.commit()


# Execute a SQL query to retrieve data
#cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
cursor.execute('SELECT * FROM devicekeys')

# Fetch the results
rows = cursor.fetchall()

# Print the results
for row in rows:
    print(row)

# Close the cursor and connection
cursor.close()
conn.close()

# ('storeversion',)
# ('accounts',)
# ('devicekeys',)
# ('encryptedrooms',)       
# ('megolminboundsessions',)
# ('forwardedchains',)      
# ('keys',)
# ('olmsessions',)
# ('outgoingkeyrequests',)  
# ('synctokens',)